import asyncio
import itertools
import json

import httpx
import pytest
from PIL import Image

from app import config, db, max_bot, max_chat, customer_accounts as accounts
from app.vault import seal, unseal, set_config
from test_admin import login
from test_customer_accounts import max_contact
from test_messengers import MAX, post_max


def buttons(body):
    return [b for a in body['attachments'] if a['type']=='inline_keyboard' for row in a['payload']['buttons'] for b in row]


class Chat:
    def __init__(self, uid=555, chat=123):
        self.uid,self.chat,self.ids=uid,chat,itertools.count()

    def event(self, action, text=None):
        event={'kind':'message_callback','chat_id':self.chat,'user_id':self.uid,
               'identity':f'{self.uid}-{next(self.ids)}','action':action,'callback_id':'cb','message_id':'nav'}
        if text is not None:event.update(kind='message_created',action='text',text=seal(text),callback_id='')
        return event

    def do(self, action, text=None):
        return max_chat.handle(MAX,self.event(action,text))

    def preview(self, method='pickup', payment='cash'):
        self.do('add:1');self.do('checkout');self.do('method:'+method)
        if method=='delivery':self.do('district:0')
        self.do('text','Игорь');self.do('text','8 999 123-45-67')
        if method=='delivery':self.do('text','Казань, Петербургская, 64, квартира 10, этаж 2')
        self.do('text','Два прибора')
        return self.do('payment:'+payment)

    def order(self, method='pickup', payment='cash'):
        preview=self.preview(method,payment)
        action=next(b['payload'] for b in buttons(preview) if b.get('payload','').startswith('confirm:'))
        return self.do(action),action


def policy():
    with db.connect(True) as c:
        c.executemany('UPDATE settings SET value=? WHERE key=?',[
            ('10','pickup_discount'),('1','delivery_enabled'),('1','delivery_districts'),
            ('30000','delivery_fee'),('140000','free_delivery_from'),('1','card_on_receipt')])


def test_native_pickup_idempotent_receipt_history_and_status(client):
    policy();chat=Chat()
    response,confirm=chat.order(payment='card')
    assert 'получен' in response['text']
    assert 'получен' in chat.do(confirm)['text']  # Second distinct callback, same confirmation.
    with db.connect() as c:
        order=c.execute('SELECT * FROM orders').fetchone()
        assert c.execute('SELECT count(*) FROM orders').fetchone()[0]==1
        assert order['channel']=='max' and order['user_id']<-(2**62)
        assert order['phone']=='+79991234567' and order['payment_method']=='card'
        assert order['discount']==order['subtotal']//10 and order['delivery']==0
        assert order['total']==order['subtotal']-order['discount']
        assert not db.cart(order['user_id'],c)
        assert c.execute('SELECT count(*) FROM max_order_messages').fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM outbox WHERE chat_id=999').fetchone()[0]==1
        assert not c.execute('SELECT 1 FROM customer_accounts').fetchone()  # Typed phone is NOT login.
    texts,_,linked=accounts.history('max',max_bot.bot_key(MAX),555)
    assert len(texts)==1 and not linked and 'Два прибора' in texts[0]
    assert not accounts.history('max',max_bot.bot_key(MAX),556)[0]
    assert not accounts.history('max','another-bot',555)[0]
    db.set_status(order['id'],'accepted')
    with db.connect() as c:assert c.execute('SELECT count(*) FROM max_order_messages').fetchone()[0]==2
    csrf=login(client)
    assert 'MAX · чат' in client.get('/admin/orders').text
    assert 'Заказы в чате MAX' in client.get('/admin/notifications').text
    assert client.post('/admin/max-notifications/1/retry',data={}).status_code==403


def test_delivery_contact_address_and_iiko_snapshot(shop):
    policy()
    iid='11111111-1111-4111-8111-111111111111'
    with db.connect(True) as c:c.execute('UPDATE products SET iiko_id=? WHERE id=1',(iid,))
    set_config('iiko',{'enabled':True})
    chat=Chat();chat.order('delivery')
    with db.connect() as c:
        order=c.execute('SELECT * FROM orders').fetchone()
        assert order['district']=='Вахитовский' and 'квартира 10' in order['address']
        assert order['delivery']==30000 and order['discount']==0
        assert c.execute('SELECT iiko_id FROM order_items').fetchone()[0]==iid
    db.set_status(order['id'],'accepted')
    from app.integrations import iiko_payload
    with db.connect() as c:job=c.execute('SELECT * FROM iiko_jobs').fetchone()
    payload=iiko_payload(job,{'organization_id':'org','terminal_group':'terminal','city_format':True,'delivery_product':iid})
    assert payload['order']['deliveryPoint']['address']['line1']==order['address']
    assert payload['order']['phone']==order['phone']


@pytest.mark.parametrize('change',['price','hidden','closed','expired','cart'])
def test_stale_confirmation_never_places_changed_order(shop,change):
    chat=Chat();preview=chat.preview()
    confirm=next(b['payload'] for b in buttons(preview) if b.get('payload','').startswith('confirm:'))
    with db.connect(True) as c:
        if change=='price':c.execute('UPDATE products SET price=price+100 WHERE id=1')
        if change=='hidden':c.execute('UPDATE products SET active=0 WHERE id=1')
        if change=='closed':c.execute("UPDATE settings SET value='0' WHERE key='orders_open'")
        if change=='expired':c.execute('UPDATE max_chat_sessions SET expires=0')
    if change=='cart':chat.do('plus:1')
    assert 'получен!' not in chat.do(confirm)['text']
    with db.connect() as c:assert not c.execute('SELECT 1 FROM orders').fetchone()


def test_retry_cart_restart_and_channel_isolation(shop):
    chat=Chat();event=chat.event('add:1')
    first=max_chat.handle(MAX,event);db.init()
    assert max_chat.handle(MAX,event)==first
    db.cart_change(555,1,3);db.cart_change(-555,1,4)
    with db.connect() as c:
        sid=c.execute('SELECT id FROM max_chat_sessions').fetchone()[0]
        assert db.cart(-(2**62+sid),c)[0]['quantity']==1
        assert db.cart(555,c)[0]['quantity']==3 and db.cart(-555,c)[0]['quantity']==4
    assert 'пуста' in Chat(556,124).do('cart')['text']
    chat.do('checkout');chat.do('method:pickup');chat.do('text','Частное имя')
    db.init();chat.do('text','89991234567')
    with db.connect() as c:
        row=c.execute('SELECT * FROM max_chat_sessions WHERE user_id=555').fetchone()
        assert row['step']=='comment' and 'Частное имя' not in row['data']
        assert unseal(row['data'])['customer']=='Частное имя'


def test_photo_categories_and_message_limits(client):
    photo='a'*32+'.jpg';Image.new('RGB',(40,40),'red').save(config.MEDIA/photo)
    with db.connect(True) as c:
        c.execute('UPDATE products SET photo=? WHERE id=1',(photo,))
        c.execute("INSERT INTO categories(name) VALUES ('Скрытая категория')")
        for i in range(35):
            cid=c.execute('INSERT INTO categories(name) VALUES (?)',(f'Раздел {i}',)).lastrowid
            c.execute("INSERT INTO products(category_id,name,ingredients,price) VALUES (?,?,'Рис',100)",(cid,'Блюдо '+str(i)))
    chat=Chat();menu=chat.do('menu')
    assert 'Скрытая категория' not in str(menu)
    assert any(b.get('payload')=='menu:1' for b in buttons(menu))
    assert len(menu['attachments'][0]['payload']['buttons'])<=30
    product=chat.do('product:1')
    assert product['attachments'][0]['payload']['url']==MAX['public_url']+'/assets/'+photo
    assert client.get('/assets/'+photo).status_code==200
    assert len(product['text'])<=4000


def test_native_account_avoids_duplicates_and_mute_preserves_history(shop):
    key=max_bot.bot_key(MAX)
    accounts.begin_contact('max',key,555,123);accounts.verify_contact('max',key,555,123,'+79991234567')
    chat=Chat();chat.order()
    with db.connect() as c:
        assert not c.execute('SELECT 1 FROM customer_messages').fetchone()
        oid=c.execute('SELECT id FROM orders').fetchone()[0]
    db.set_status(oid,'accepted')
    with db.connect() as c:
        assert not c.execute('SELECT 1 FROM customer_messages').fetchone()
        assert c.execute('SELECT count(*) FROM max_order_messages').fetchone()[0]==2
    max_chat.notifications(key,555,False);db.set_status(oid,'cooking')
    assert len(accounts.history('max',key,555)[0])==1
    max_chat.notifications(key,555,True);db.set_status(oid,'ready')
    with db.connect() as c:assert c.execute("SELECT count(*) FROM max_order_messages WHERE event='ready:0'").fetchone()[0]==1


def test_webhook_text_encrypted_and_native_contact_does_not_login(client,monkeypatch):
    set_config('max',MAX);chat=Chat()
    chat.do('add:1');chat.do('checkout');chat.do('method:pickup')
    calls=[]
    async def api(cfg,method,path,body=None,params=None):
        calls.append((method,path,body,params));return {'message':{'body':{'mid':'nav'}}}
    monkeypatch.setattr(max_bot,'api',api)
    event=max_contact('name');event['message']['body']={'mid':'name','text':'Частное имя'}
    assert post_max(client,event).status_code==200
    with db.connect() as c:
        stored=c.execute('SELECT payload FROM max_events').fetchone()[0]
        assert 'Частное имя' not in stored and unseal(json.loads(stored)['text'])=='Частное имя'
    asyncio.run(max_bot.tick())
    assert 'телефон' in calls[-1][2]['text']
    assert post_max(client,max_contact()).status_code==200
    asyncio.run(max_bot.tick())
    assert 'Комментарий' in calls[-1][2]['text']
    with db.connect() as c:
        assert not c.execute('SELECT 1 FROM customer_accounts').fetchone()
        assert unseal(c.execute('SELECT data FROM max_chat_sessions').fetchone()[0])['phone']=='+79991234567'


def test_navigation_failure_replays_without_second_cart_mutation(client,monkeypatch):
    set_config('max',MAX)
    event={'update_type':'message_callback','timestamp':1,
        'callback':{'callback_id':'add','payload':'add:1','user':{'user_id':555}},
        'message':{'recipient':{'chat_id':123,'chat_type':'dialog'},'body':{'mid':'nav'}}}
    with db.connect(True) as c:c.execute('INSERT INTO max_screens VALUES (?,?,?)',(max_bot.bot_key(MAX),123,'nav'))
    failed=True
    async def api(*args):
        if failed:raise httpx.ConnectError('no network')
        return {'success':True}
    monkeypatch.setattr(max_bot,'api',api)
    post_max(client,event);asyncio.run(max_bot.tick())
    with db.connect(True) as c:
        assert c.execute('SELECT quantity FROM cart').fetchone()[0]==1
        c.execute('UPDATE max_events SET next_try=0')
    failed=False;asyncio.run(max_bot.tick())
    with db.connect() as c:
        assert c.execute('SELECT quantity FROM cart').fetchone()[0]==1
        assert c.execute('SELECT state FROM max_events').fetchone()[0]=='done'


def test_outbox_ambiguous_send_not_retried_and_admin_recovery(client,monkeypatch):
    Chat().order()
    async def api(*args):raise httpx.ReadTimeout('secret token must not leak')
    monkeypatch.setattr(max_bot,'api',api)
    asyncio.run(max_bot.deliver_customer_once(MAX,native=True))
    with db.connect() as c:
        row=c.execute('SELECT * FROM max_order_messages').fetchone()
        assert row['sent']==-1 and 'secret token' not in row['error']
    assert not max_chat.claim(max_bot.bot_key(MAX))
    csrf=login(client)
    assert client.post(f'/admin/max-notifications/{row["id"]}/retry',data={'csrf':csrf}).status_code==200
    assert max_chat.claim(max_bot.bot_key(MAX))


def test_photo_failure_keeps_order_button(shop,monkeypatch):
    photo='b'*32+'.jpg';Image.new('RGB',(20,20),'red').save(config.MEDIA/photo)
    with db.connect(True) as c:
        c.execute('UPDATE products SET photo=? WHERE id=1',(photo,))
        c.execute('INSERT INTO max_screens VALUES (?,?,?)',(max_bot.bot_key(MAX),123,'nav'))
    calls=[]
    async def api(cfg,method,path,body=None,params=None):
        calls.append(body)
        if len(calls)==1:
            raise httpx.HTTPStatusError('image unavailable',request=httpx.Request('POST','https://example.test'),response=httpx.Response(400))
        return {'success':True}
    monkeypatch.setattr(max_bot,'api',api)
    asyncio.run(max_bot.process_event(MAX,Chat().event('product:1')))
    assert len(calls)==2 and 'Фото временно недоступно' in calls[1]['message']['text']
    assert any(b.get('payload')=='add:1' for b in buttons(calls[1]['message']))


def test_adapter_entire_checkout_and_receipt_never_becomes_navigation(client,monkeypatch):
    set_config('max',MAX);calls=[]
    async def api(cfg,method,path,body=None,params=None):
        calls.append((method,path,body,params))
        return {'message':{'body':{'mid':f'mid-{len(calls)}'}}}
    monkeypatch.setattr(max_bot,'api',api)
    sequence=['/menu','add:1','checkout','method:pickup','Игорь','89991234567','skip','payment:cash']
    for i,value in enumerate(sequence):
        message={'sender':{'user_id':555},'recipient':{'chat_id':123,'chat_type':'dialog'},'body':{'mid':str(i)}}
        if ':' in value or value in ('checkout','skip'):
            event={'update_type':'message_callback','timestamp':i,'message':message,
                'callback':{'callback_id':str(i),'payload':value,'user':{'user_id':555}}}
        else:
            message['body']['text']=value
            event={'update_type':'message_created','timestamp':i,'message':message}
        assert post_max(client,event).status_code==200
        asyncio.run(max_bot.tick())
    preview=calls[-1][2]
    confirm=next(b['payload'] for b in buttons(preview) if b.get('payload','').startswith('confirm:'))
    event['callback'].update(callback_id='confirm',payload=confirm)
    post_max(client,event);asyncio.run(max_bot.tick());asyncio.run(max_bot.tick())
    assert 'Спасибо!' in calls[-1][2]['text']
    assert calls[-1][0]=='POST' and calls[-1][3]=={'chat_id':123}
    receipt_mid=f'mid-{len(calls)}'
    event['callback'].update(callback_id='menu-after',payload='menu')
    post_max(client,event);asyncio.run(max_bot.tick())
    assert calls[-1][0]=='PUT' and calls[-1][3]['message_id']!=receipt_mid
