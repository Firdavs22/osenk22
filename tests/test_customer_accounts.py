import asyncio
import hashlib
import hmac
import json
import ssl
import time
from datetime import datetime, timezone

import httpx
import pytest
from aiogram import Bot
from aiogram.types import Message

from app import db, customer_accounts as accounts, max_bot
from app.bot import commands, checkout_text, show_orders, show_menu, deliver_once
from app.vault import set_config
from test_bot import FakeTelegram
from test_order_updates import order, setup_bot, TOKEN
from test_messengers import MAX, post_max, started
from test_store import draft, session


def link(platform='telegram',key=123456,uid=321,chat=321,phone='+79991234567'):
    accounts.begin_contact(platform,key,uid,chat)
    return accounts.verify_contact(platform,key,uid,chat,phone)


def message(bot, text=None, contact=None, forwarded=False):
    values=dict(message_id=900,date=datetime.now(timezone.utc),chat={'id':321,'type':'private'},
                from_user={'id':321,'is_bot':False,'first_name':'Test'},text=text,contact=contact)
    if forwarded:
        values['forward_origin']={'type':'hidden_user','date':datetime.now(timezone.utc),'sender_user_name':'Someone'}
    return Message(**values).as_(bot)


def test_telegram_only_own_contact_unlocks_site_orders_and_history_is_persistent(client):
    setup_bot();order(client)
    async def run():
        fake=FakeTelegram();bot=Bot(TOKEN,session=fake)
        await commands(message(bot,'/start account'))
        assert fake.calls[-1].reply_markup.keyboard[0][0].request_contact
        await checkout_text(message(bot,'+79991234567'))
        await checkout_text(message(bot,contact={'phone_number':'+79991234567','first_name':'X','user_id':999}))
        await checkout_text(message(bot,contact={'phone_number':'+79991234567','first_name':'X','user_id':321},forwarded=True))
        with db.connect() as c:assert not c.execute('SELECT 1 FROM customer_accounts').fetchone()
        await checkout_text(message(bot,contact={'phone_number':'89991234567','first_name':'X','user_id':321}))
        assert accounts.history('telegram',123456,321)[2]
        history_ids=[i+1 for i,m in enumerate(fake.calls) if m.__api_method__=='sendMessage' and 'Заказ №1' in m.text]
        assert history_ids
        await show_menu(message(bot,'/menu'))
        assert not any(m.__api_method__ in ('deleteMessage','editMessageText') and m.message_id in history_ids for m in fake.calls)
        await bot.session.close()
    asyncio.run(run())
    assert not accounts.history('telegram',123456,322)[0]
    assert not accounts.history('telegram',777,321)[0]


def test_phone_normalization_migration_and_no_cross_phone_history(client):
    order(client)
    with db.connect(True) as c:c.execute("UPDATE orders SET phone='8 (999) 123-45-67',phone_key=''")
    db.init();db.init()
    link(phone='9991234567')
    assert len(accounts.history('telegram',123456,321)[0])==1
    link(uid=322,chat=322,phone='+79990000000')
    assert not accounts.history('telegram',123456,322)[0]
    assert accounts.normalize_phone('123')==''
    with pytest.raises(ValueError):link(uid=323,chat=323)


def test_expired_or_unsolicited_contact_and_changed_phone_cannot_bind(shop):
    with pytest.raises(ValueError):accounts.verify_contact('telegram',123456,321,321,'+79991234567')
    accounts.begin_contact('telegram',123456,321,321)
    with db.connect(True) as c:c.execute('UPDATE customer_contact_requests SET expires=0')
    with pytest.raises(ValueError):accounts.verify_contact('telegram',123456,321,321,'+79991234567')
    link()
    with pytest.raises(ValueError):link(phone='+79990000000')
    accounts.stop('telegram',123456,321,logout=True)
    link(phone='+79990000000')


def test_future_orders_statuses_stop_and_logout_preserve_orders(client):
    setup_bot();link();url,_=order(client)
    db.set_status(1,'accepted')
    async def run():
        fake=FakeTelegram();bot=Bot(TOKEN,session=fake)
        await deliver_once(bot);await deliver_once(bot);await deliver_once(bot)
        receipts=[m for m in fake.calls if m.__api_method__=='sendMessage' and m.chat_id==321]
        assert len(receipts)==2 and 'Заказ получен' in receipts[0].text and 'Принят' in receipts[1].text
        await bot.session.close()
    asyncio.run(run())
    accounts.stop('telegram',123456,321)
    db.set_status(1,'cooking')
    assert accounts.history('telegram',123456,321)[0]
    with db.connect() as c:assert c.execute('SELECT count(*) FROM customer_messages').fetchone()[0]==2
    accounts.stop('telegram',123456,321,logout=True)
    assert not accounts.history('telegram',123456,321)[0]
    assert client.get(url).status_code==200
    link();assert accounts.history('telegram',123456,321)[0]


def test_history_pagination_and_stop_does_not_disable_history(client):
    setup_bot();link()
    for _ in range(7):order(client)
    texts,cursor,_=accounts.history('telegram',123456,321)
    assert len(texts)==5 and cursor==3
    older,more,_=accounts.history('telegram',123456,321,cursor)
    assert len(older)==2 and more==0
    accounts.stop('telegram',123456,321)
    assert len(accounts.history('telegram',123456,321)[0])==5
    with db.connect() as c:assert not c.execute('SELECT 1 FROM customer_messages WHERE sent=0').fetchone()


def max_contact(mid='contact-1'):
    vcard='BEGIN:VCARD\r\nVERSION:3.0\r\nTEL;TYPE=cell:79991234567\r\nFN:Private name\r\nEND:VCARD\r\n'
    return {'update_type':'message_created','timestamp':int(time.time()*1000),
        'message':{'sender':{'user_id':555,'is_bot':False},'recipient':{'chat_id':123,'chat_type':'dialog'},
            'body':{'mid':mid,'attachments':[{'type':'contact','payload':{'vcf_info':vcard,
                'max_info':{'user_id':555},'hash':hmac.new(MAX['token'].encode(),vcard.encode(),hashlib.sha256).hexdigest()}}]}}}


@pytest.mark.parametrize('change',['hash','number','sender','forward','multiple'])
def test_max_forged_contacts_are_rejected(change):
    event=max_contact();p=event['message']['body']['attachments'][0]['payload']
    assert accounts.max_contact_phone(event,MAX['token'])=='+79991234567'
    if change=='hash':p.pop('hash')
    if change=='number':p['vcf_info']=p['vcf_info'].replace('79991234567','79990000000')
    if change=='sender':event['message']['sender']['user_id']=666
    if change=='forward':event['message']['link']={'type':'forward'}
    if change=='multiple':event['message']['body']['attachments']*=2
    assert not accounts.max_contact_phone(event,MAX['token'])


def test_max_verified_contact_events_history_and_persistent_notifications(client,monkeypatch):
    set_config('max',MAX);order(client)
    calls=[]
    async def api(cfg,method,path,body=None,params=None):
        calls.append((method,path,body,params))
        return {'message':{'body':{'mid':f'mid-{len(calls)}'}}}
    monkeypatch.setattr(max_bot,'api',api)
    start=started();start['payload']='account'
    post_max(client,start);asyncio.run(max_bot.tick())
    assert calls[-1][2]['attachments'][0]['payload']['buttons'][0][0]['type']=='request_contact'
    post_max(client,max_contact());post_max(client,max_contact())
    with db.connect() as c:
        payloads=str(c.execute('SELECT payload FROM max_events').fetchall())
        assert '79991234567' not in payloads and 'Private name' not in payloads
    asyncio.run(max_bot.tick())
    assert accounts.history('max',max_bot.bot_key(MAX),555)[2]
    hist={'kind':'message_callback','chat_id':123,'user_id':555,'callback_id':'hist','message_id':'mid-1','action':'orders'}
    asyncio.run(max_bot.process_event(MAX,hist))
    assert calls[-1][0]=='POST' and 'Заказ №1' in calls[-1][2]['text']
    history_mid=f'mid-{len(calls)}'
    back=dict(hist,identity='back',callback_id='back',message_id=history_mid,action='menu')
    asyncio.run(max_bot.process_event(MAX,back))
    assert not any(method in ('PUT','DELETE') and params.get('message_id')==history_mid for method,path,body,params in calls)
    assert not any(path=='/answers' and params['callback_id']=='back' and 'message' in body for method,path,body,params in calls)
    db.set_status(1,'accepted');asyncio.run(max_bot.tick());asyncio.run(max_bot.tick())
    assert len([x for x in calls if x[0]=='POST' and x[1]=='/messages' and 'Принят' in x[2]['text']])==1


def test_max_delivery_timeout_stops_auto_retry_and_bot_change_isolated(client,monkeypatch):
    set_config('max',MAX);link('max',max_bot.bot_key(MAX),555,123);order(client)
    async def api(*args,**kwargs):raise httpx.ReadTimeout('PRIVATE TOKEN')
    monkeypatch.setattr(max_bot,'api',api)
    asyncio.run(max_bot.tick())
    with db.connect() as c:
        row=c.execute('SELECT * FROM customer_messages').fetchone()
        assert row['sent']==-1 and 'PRIVATE' not in row['error']
    assert not accounts.history('max','other-bot',555)[0]


def test_order_page_has_both_accounts_only_when_configured(client):
    setup_bot();url,_=order(client)
    assert 'Личный кабинет в Telegram' in client.get(url).text
    assert 'Личный кабинет в MAX' not in client.get(url).text
    set_config('max',MAX|{'bot_username':'shop_bot','subscribed':True})
    page=client.get(url).text
    assert 'https://max.ru/shop_bot?start=account' in page
    assert '79991234567' not in accounts.links()['max']


def test_max_errors_distinguish_tls_and_credentials_without_secrets(client,monkeypatch):
    from test_admin import login
    csrf=login(client);set_config('max',MAX)
    async def bad_ssl(*args,**kwargs):
        try:raise ssl.SSLCertVerificationError('CERTIFICATE_VERIFY_FAILED PRIVATE TOKEN')
        except ssl.SSLError as exc:raise httpx.ConnectError('PRIVATE TOKEN') from exc
    monkeypatch.setattr(max_bot,'api',bad_ssl)
    text=client.post('/admin/integrations/max/check',data={'csrf':csrf}).text
    assert 'MAX_CA_BUNDLE' in text and 'цепочке TLS' in text and 'PRIVATE TOKEN' not in text
    exc=httpx.HTTPStatusError('PRIVATE TOKEN',request=httpx.Request('GET','https://example.test'),response=httpx.Response(401))
    assert '401' in max_bot.connection_error(exc) and 'PRIVATE' not in max_bot.connection_error(exc)


def test_upgrade_preserves_old_history_messages_and_navigation_on_next_restart(shop):
    with db.connect(True) as c:
        for table in ('customer_messages','customer_orders','customer_contact_requests','customer_accounts'):
            c.execute('DROP TABLE '+table)
        c.execute('INSERT INTO telegram_screens VALUES (?,?,?)',(123456,321,'[{"id":20,"kind":"text"}]'))
    db.init()
    with db.connect(True) as c:
        assert not c.execute('SELECT 1 FROM telegram_screens').fetchone()
        c.execute('INSERT INTO telegram_screens VALUES (?,?,?)',(123456,321,'[{"id":21,"kind":"text"}]'))
    db.init()
    with db.connect() as c:assert c.execute('SELECT 1 FROM telegram_screens').fetchone()


def test_customer_delivery_keeps_order_when_previous_message_retries(client):
    setup_bot();link();order(client);db.set_status(1,'accepted')
    first=accounts.claim('telegram',123456)
    assert first and 'Заказ получен' in first['text']
    assert accounts.claim('telegram',123456) is None
    accounts.complete(first['id'],1)
    assert 'Принят' in accounts.claim('telegram',123456)['text']
