import asyncio
import re
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit, parse_qs

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from app import db, order_updates as updates
from app.bot import commands, deliver_once
from app.integrations import iiko_payload
from app.vault import set_config
from test_admin import login
from test_bot import FakeTelegram
from test_store import session, draft

TOKEN='123456:TEST_TOKEN_NOT_USED_FOR_NETWORK'


def setup_bot():
    set_config('telegram',{'enabled':True,'token':TOKEN,'admin_ids':[999]})
    updates.record_bot(123456,'sushi_test_bot',TOKEN)


def order(client):
    headers=session(client)
    url=client.post('/api/checkout',headers=headers,json=draft(client,headers)).json()['url']
    return url,headers['x-csrf-token']


def ticket(client,url,csrf):
    response=client.post(url+'/telegram',data={'csrf':csrf},follow_redirects=False)
    assert response.status_code==200
    link=re.search(r'href="(https://t.me/[^\"]+)"',response.text).group(1)
    target=urlsplit(link)
    assert target.netloc=='t.me' and url not in link
    return parse_qs(target.query)['start'][0][6:]


def test_guest_subscribes_with_one_time_ticket_and_receives_status(client):
    setup_bot();url,csrf=order(client)
    raw=ticket(client,url,csrf)
    with db.connect() as c:
        assert raw not in c.execute('SELECT token_hash FROM order_subscription_tickets').fetchone()[0]
    async def run():
        transport=FakeTelegram();bot=Bot(TOKEN,session=transport)
        message=Message(message_id=1,date=datetime.now(timezone.utc),chat={'id':321,'type':'private'},
                        from_user={'id':321,'is_bot':False,'first_name':'Test'},text='/start watch_'+raw).as_(bot)
        await commands(message)
        db.set_status(1,'accepted')
        await deliver_once(bot)
        status=[m for m in transport.calls if m.__api_method__=='sendMessage' and m.chat_id==321 and 'Принят' in m.text]
        assert len(status)==1
        await bot.session.close()
    asyncio.run(run())
    with pytest.raises(ValueError):updates.subscribe(raw,322,123456)
    payload=client.get('/api'+url+'/status').json()
    assert payload['status']=='accepted' and payload['subscribed'] is True
    assert not any(key in payload for key in ('customer','phone','address','user_id'))
    assert 'Уведомления об этом заказе подключены' in client.get(url).text


def test_ticket_requires_csrf_and_expires_and_does_not_rebind(client):
    setup_bot();url,csrf=order(client)
    assert client.post(url+'/telegram').status_code==403
    assert client.get('/api/order/1/status').status_code==404
    raw=ticket(client,url,csrf)
    with pytest.raises(ValueError):updates.subscribe(raw,321,888)
    with db.connect(True) as c:c.execute('UPDATE order_subscription_tickets SET expires=?',(time.time()-1,))
    with pytest.raises(ValueError):updates.subscribe(raw,321,123456)
    raw=ticket(client,url,csrf);updates.subscribe(raw,321,123456)
    raw=ticket(client,url,csrf)
    with pytest.raises(ValueError):updates.subscribe(raw,322,123456)
    with db.connect() as c:assert c.execute('SELECT chat_id FROM order_subscriptions').fetchone()[0]==321


@pytest.mark.parametrize('via_web',[True,False])
def test_unsubscribe_cancels_queued_status_and_future_updates(client,via_web):
    setup_bot();url,csrf=order(client)
    updates.subscribe(ticket(client,url,csrf),321,123456)
    db.set_status(1,'accepted')
    if via_web:
        assert client.post(url+'/telegram',data={'csrf':csrf,'action':'stop'}).status_code==200
    else:updates.unsubscribe_chat(321,123456)
    db.set_status(1,'cooking')
    with db.connect() as c:
        assert not c.execute('SELECT 1 FROM outbox WHERE chat_id=321').fetchone()
        assert not c.execute('SELECT 1 FROM order_subscriptions').fetchone()


def test_no_recipients_does_not_mark_notification_sent_and_recovers_once(client):
    set_config('telegram',{'enabled':True,'token':TOKEN,'admin_ids':[]})
    order(client)
    with db.connect() as c:
        assert c.execute('SELECT notified FROM orders').fetchone()[0]==0
        assert not c.execute('SELECT 1 FROM outbox').fetchone()
    setup_bot()
    async def run():
        transport=FakeTelegram();bot=Bot(TOKEN,session=transport)
        await deliver_once(bot);await deliver_once(bot)
        assert len([m for m in transport.calls if m.__api_method__=='sendMessage'])==1
        await bot.session.close()
    asyncio.run(run())
    with db.connect() as c:
        assert c.execute('SELECT notified FROM orders').fetchone()[0]==1
        assert c.execute('SELECT sent FROM outbox').fetchone()[0]==1


def test_diagnostics_show_bad_recipient_without_provider_body(client):
    setup_bot();order(client)
    class BadChat(FakeTelegram):
        async def make_request(self,bot,method,timeout=None):
            raise TelegramBadRequest(method=method,message='SECRET token address phone')
    async def run():
        bot=Bot(TOKEN,session=BadChat());await deliver_once(bot);await bot.session.close()
    asyncio.run(run());login(client)
    page=client.get('/admin/notifications')
    assert 'Проверьте ID получателя' in page.text and 'SECRET' not in page.text and TOKEN not in page.text
    assert 'Служба отправки работает' in page.text
    with db.connect(True) as c:c.execute('UPDATE telegram_runtime SET heartbeat=0')
    assert 'Нет свежего сигнала' in client.get('/admin/notifications').text


def test_changed_bot_cannot_receive_old_subscription(client):
    setup_bot();url,csrf=order(client);updates.subscribe(ticket(client,url,csrf),321,123456)
    db.set_status(1,'accepted')
    async def run():
        transport=FakeTelegram();bot=Bot('654321:ANOTHER_TOKEN_NOT_USED_FOR_NETWORK',session=transport)
        await deliver_once(bot)
        assert not any(m.chat_id==321 for m in transport.calls)
        await bot.session.close()
    asyncio.run(run())


def test_iiko_pickup_omits_delivery_address_and_delivery_carries_contacts(client):
    url,csrf=order(client)
    cfg={'organization_id':str(uuid.uuid4()),'terminal_group':str(uuid.uuid4()),'city_format':True}
    with db.connect(True) as c:c.execute('UPDATE order_items SET iiko_id=?',(str(uuid.uuid4()),))
    row={'order_id':1,'external_id':str(uuid.uuid4())}
    pickup=iiko_payload(row,cfg)['order']
    assert pickup['orderServiceType']=='DeliveryByClient' and 'deliveryPoint' not in pickup
    with db.connect(True) as c:
        c.execute("UPDATE orders SET method='delivery',address=?,district=? WHERE id=1",('Казань, улица Примерная, дом 5, квартира 10','Вахитовский'))
    delivery=iiko_payload(row,cfg)['order']
    assert delivery['orderServiceType']=='DeliveryByCourier'
    assert delivery['deliveryPoint']['address']=={'type':'city','line1':'Казань, улица Примерная, дом 5, квартира 10'}
    assert delivery['phone']=='+79991234567' and delivery['customer']['name']=='Покупатель'
    assert 'Без звонка в дверь' in delivery['comment']
    with pytest.raises(ValueError):iiko_payload(row,cfg|{'city_format':False})
