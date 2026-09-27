import asyncio
import base64
import json
import re
from datetime import datetime, timezone

import httpx
import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, ReplyKeyboardRemove
from itsdangerous import TimestampSigner
from PIL import Image

from app import config, db, max_bot
from app.bot import home_keyboard, show_menu
from app.chat_screen import screen
from app.mini_apps import app_url, public_origin
from app.vault import get_config, set_config
from test_admin import login
from test_bot import FakeTelegram
from test_store import draft

MAX = {'token':'private-max-token','webhook_secret':'a'*64,'enabled':True,'public_url':'https://shop.example'}


def started(stamp=1750000000000):
    return {'update_type':'bot_started','timestamp':stamp,'chat_id':123,'user':{'user_id':555,'first_name':'Private name'}}


def post_max(client, event=None):
    return client.post('/api/max/webhook',headers={'X-Max-Bot-Api-Secret':MAX['webhook_secret']},json=event or started())


@pytest.mark.parametrize('platform', ['telegram','max'])
def test_mini_app_guest_order_uses_same_catalog_and_is_not_messenger_auth(client, platform):
    page=client.get('/mini/'+platform)
    assert page.status_code==200 and 'Филадельфия' in page.text
    sdk='https://telegram.org' if platform=='telegram' else 'https://st.max.ru'
    assert sdk in page.text and sdk in page.headers['content-security-policy']
    assert "frame-ancestors 'none'" not in page.headers['content-security-policy']
    assert 'x-frame-options' not in page.headers
    token=re.search(r'name="csrf-token" content="([^"]+)"',page.text).group(1)
    headers={'x-csrf-token':token}
    data=draft(client,headers)
    data.update(user_id=999,initDataUnsafe={'user':{'id':999}})
    response=client.post('/api/checkout',headers=headers,json=data)
    assert response.status_code==200 and client.get(response.json()['url']).status_code==200
    with db.connect() as c:
        order=c.execute('SELECT * FROM orders').fetchone()
        assert order['channel']==platform+'_app' and order['user_id']<0
        assert c.execute('SELECT count(*) FROM outbox').fetchone()[0]==1  # Shop admin only.
    assert client.get('/admin',follow_redirects=False).status_code==303
    assert client.get('/admin/login').headers['x-frame-options']=='DENY'
    assert sdk not in client.get('/admin/login').headers['content-security-policy']


def test_guest_session_cannot_be_reused_as_admin_and_legacy_cart_migrates(client):
    old={'visitor':-12345,'csrf':'old-csrf','checkout_key':'old-key','admin':'admin','credential':config.ADMIN_PASSWORD_HASH[-32:]}
    value=TimestampSigner(config.SESSION_SECRET).sign(base64.b64encode(json.dumps(old).encode())).decode()
    client.cookies.set('sushi_admin',value)
    db.cart_change(-12345,1,1)
    assert client.get('/api/cart').json()['quantity']==1
    store_cookie=client.cookies.get('sushi_store')
    payload=json.loads(base64.b64decode(TimestampSigner(config.SESSION_SECRET+':store').unsign(store_cookie)))
    assert payload=={k:old[k] for k in ('visitor','csrf','checkout_key')}
    client.cookies.clear()
    client.cookies.set('sushi_admin',store_cookie)
    assert client.get('/admin',follow_redirects=False).status_code==303


def test_telegram_navigation_edits_text_and_replaces_media_only(shop,tmp_path):
    async def run():
        session=FakeTelegram();bot=Bot('123456:TEST_TOKEN_NOT_USED_FOR_NETWORK',session=session)
        message=Message(message_id=900,date=datetime.now(timezone.utc),chat={'id':100,'type':'private'},text='/menu').as_(bot)
        await screen(message,'Меню',home_keyboard())
        await screen(message,'Категория',home_keyboard())
        assert [m.__api_method__ for m in session.calls]==['sendMessage','editMessageText']
        photo=tmp_path/'dish.jpg';Image.new('RGB',(30,30),'red').save(photo)
        await screen(message,'Блюдо',home_keyboard(),photo)
        await screen(message,'Корзина',home_keyboard())
        deletes=[m.message_id for m in session.calls if m.__api_method__=='deleteMessage']
        assert 900 not in deletes and len(deletes)==2
        assert sum(m.__api_method__=='sendPhoto' for m in session.calls)==1
        assert sum(m.__api_method__=='sendMessage' for m in session.calls)==2
        # Long text keeps every part, instead of deleting the preceding chunk.
        await screen(message,'x'*7100,home_keyboard())
        with db.connect() as c:
            saved=json.loads(c.execute('SELECT messages FROM telegram_screens').fetchone()[0])
        assert len(saved)==3
        await screen(message,'Телефон',ReplyKeyboardRemove())
        assert all(item['id'] in [m.message_id for m in session.calls if m.__api_method__=='deleteMessage'] for item in saved)
        await bot.session.close()
    asyncio.run(run())


def test_telegram_unchanged_and_deleted_screen_fallback(shop):
    class Telegram(FakeTelegram):
        failure='message is not modified'
        async def make_request(self,bot,method,timeout=None):
            if method.__api_method__=='editMessageText':
                self.calls.append(method)
                raise TelegramBadRequest(method=method,message=self.failure)
            return await super().make_request(bot,method,timeout)
    async def run():
        session=Telegram();bot=Bot('123456:TEST_TOKEN_NOT_USED_FOR_NETWORK',session=session)
        message=Message(message_id=900,date=datetime.now(timezone.utc),chat={'id':100,'type':'private'}).as_(bot)
        await screen(message,'Меню');await screen(message,'Меню')
        assert sum(m.__api_method__=='sendMessage' for m in session.calls)==1
        session.failure='message to edit not found'
        await screen(message,'Меню')
        assert sum(m.__api_method__=='sendMessage' for m in session.calls)==2
        await bot.session.close()
    asyncio.run(run())


def test_telegram_mini_app_button_and_hidden_categories(shop):
    set_config('telegram',{'mini_app':True,'public_url':'https://shop.example'})
    assert app_url('telegram')=='https://shop.example/mini/telegram'
    assert home_keyboard().inline_keyboard[0][0].web_app.url.endswith('/mini/telegram')
    with db.connect(True) as c:
        c.execute("INSERT INTO categories(name) VALUES ('Пустая категория')")
    async def run():
        session=FakeTelegram();bot=Bot('123456:TEST_TOKEN_NOT_USED_FOR_NETWORK',session=session)
        message=Message(message_id=900,date=datetime.now(timezone.utc),chat={'id':100,'type':'private'}).as_(bot)
        await show_menu(message)
        markup=session.calls[-1].reply_markup
        assert not any(button.text=='Пустая категория' for row in markup.inline_keyboard for button in row)
        await bot.session.close()
    asyncio.run(run())


@pytest.mark.parametrize('url',['http://shop.example','https://user:pass@shop.example','https://shop.example/path','https://shop.example/?x=1','https://shop.example:444'])
def test_mini_app_origin_validation(url):
    with pytest.raises(ValueError):public_origin(url)


def test_max_webhook_secret_dedupe_and_minimal_storage(client,monkeypatch):
    set_config('max',MAX)
    assert client.post('/api/max/webhook',json=started()).status_code==403
    assert post_max(client).status_code==200 and post_max(client).status_code==200
    with db.connect() as c:
        rows=c.execute('SELECT * FROM max_events').fetchall()
        assert len(rows)==1 and 'Private name' not in rows[0]['payload']
    calls=[]
    async def api(cfg,method,path,body=None,params=None):
        calls.append((method,path,body,params))
        return {'message':{'body':{'mid':'mid-1'}}}
    monkeypatch.setattr(max_bot,'api',api)
    asyncio.run(max_bot.tick());asyncio.run(max_bot.tick())
    assert len(calls)==1 and calls[0][0]=='POST' and calls[0][3]=={'chat_id':123}
    assert 'https://shop.example/mini/max' in json.dumps(calls[0][2])
    post_max(client,started(1750000000100));asyncio.run(max_bot.tick())
    assert calls[-1][0]=='PUT' and calls[-1][3]=={'message_id':'mid-1'}


def test_max_callback_edits_message_and_ignores_groups(client,monkeypatch):
    set_config('max',MAX)
    event={'update_type':'message_callback','timestamp':1750000000000,
           'callback':{'callback_id':'callback-1','payload':'info'},
           'message':{'recipient':{'chat_id':123,'chat_type':'chat'},'body':{'mid':'mid-1'}}}
    post_max(client,event)
    with db.connect() as c:assert not c.execute('SELECT 1 FROM max_events').fetchone()
    with db.connect(True) as c:
        c.execute('INSERT INTO max_screens VALUES (?,?,?)',(max_bot.bot_key(MAX),123,'mid-1'))
    event['message']['recipient']['chat_type']='dialog';post_max(client,event)
    calls=[]
    async def api(*args):calls.append(args);return {'success':True}
    monkeypatch.setattr(max_bot,'api',api);asyncio.run(max_bot.tick())
    assert calls[0][1:3]==('POST','/answers') and 'Адрес:' in calls[0][3]['message']['text']


def test_max_timeout_does_not_duplicate_unknown_send(client,monkeypatch):
    set_config('max',MAX);post_max(client)
    async def api(*args):raise httpx.ReadTimeout('private-max-token')
    monkeypatch.setattr(max_bot,'api',api);asyncio.run(max_bot.tick())
    with db.connect() as c:
        row=c.execute('SELECT * FROM max_events').fetchone()
        assert row['state']=='failed' and 'private-max-token' not in row['error']


def test_max_settings_and_registration_keep_secrets_private(client,monkeypatch):
    csrf=login(client)
    assert client.post('/admin/integrations/max',data={'enabled':'on'}).status_code==403
    page=client.post('/admin/integrations/max',data={'csrf':csrf,'token':MAX['token'],'enabled':'on','public_url':MAX['public_url']})
    cfg=get_config('max')
    assert len(cfg['webhook_secret'])==64 and MAX['token'] not in page.text and cfg['webhook_secret'] not in page.text
    calls=[]
    async def api(cfg,method,path,body=None,params=None):
        calls.append((method,path,body))
        return {'username':'shop_bot'} if path=='/me' else {'success':True}
    monkeypatch.setattr(max_bot,'api',api)
    response=client.post('/admin/integrations/max/subscribe',data={'csrf':csrf})
    assert response.status_code==200 and get_config('max')['subscribed']
    body=calls[-1][2]
    assert body['url']=='https://shop.example/api/max/webhook' and body['secret']==cfg['webhook_secret']
    assert body['update_types']==max_bot.EVENT_TYPES
    assert cfg['webhook_secret'] not in response.text


def test_production_cookies_keep_admin_strict(shop,monkeypatch):
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient
    from app.mini_apps import ShopSessions
    async def touch(request):
        request.session['seen']=True
        return JSONResponse({'ok':True})
    monkeypatch.setattr(config,'COOKIE_SECURE',True)
    app=ShopSessions(Starlette(routes=[Route('/mini/telegram',touch),Route('/admin/login',touch)]))
    with TestClient(app,base_url='https://shop.example') as client:
        public=client.get('/mini/telegram').headers['set-cookie'].lower()
        admin=client.get('/admin/login').headers['set-cookie'].lower()
    assert 'sushi_store=' in public and 'samesite=none' in public and 'secure' in public and 'httponly' in public
    assert 'sushi_admin=' in admin and 'samesite=strict' in admin and 'secure' in admin


def test_max_api_keeps_token_in_header_and_checks_success(shop,monkeypatch):
    import ssl
    observed=[]
    class Client:
        def __init__(self,**kwargs):
            assert isinstance(kwargs['verify'],ssl.SSLContext)
            assert kwargs['verify'].verify_mode==ssl.CERT_REQUIRED
            assert kwargs['follow_redirects'] is False
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def request(self,method,url,**kwargs):
            observed.append((url,kwargs))
            return httpx.Response(200,json={'success':False},request=httpx.Request(method,url))
    monkeypatch.setattr(max_bot.httpx,'AsyncClient',Client)
    with pytest.raises(ValueError):asyncio.run(max_bot.api(MAX,'POST','/messages',{'text':'test'},{'chat_id':123}))
    url,args=observed[0]
    assert url==max_bot.API+'/messages' and MAX['token'] not in url
    assert args['headers']=={'Authorization':MAX['token']}


def test_typing_instead_of_checkout_buttons_keeps_navigation(shop):
    from app.bot import checkout_text
    async def run():
        session=FakeTelegram();bot=Bot('123456:TEST_TOKEN_NOT_USED_FOR_NETWORK',session=session)
        message=Message(message_id=900,date=datetime.now(timezone.utc),chat={'id':100,'type':'private'},
                        from_user={'id':100,'is_bot':False,'first_name':'Test'},text='что дальше').as_(bot)
        for step,expected in [('method','method:pickup'),('district','district:0')]:
            db.save_draft(100,step,{})
            await checkout_text(message)
            assert any(b.callback_data==expected for row in session.calls[-1].reply_markup.inline_keyboard for b in row)
        await bot.session.close()
    asyncio.run(run())
