"""MAX webhook adapter: shared guest storefront, durable events, editable navigation."""
import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import ssl
import time

import httpx
from fastapi import APIRouter, HTTPException, Request

from . import config, db
from .mini_apps import public_origin
from .vault import get_config, set_config

router = APIRouter()
log = logging.getLogger(__name__)
API = 'https://platform-api2.max.ru'
EVENT_TYPES = ['bot_started','message_created','message_callback']


class DeliveryUnknown(Exception):
    """A send may have succeeded; do not duplicate it automatically."""


def bot_key(cfg):
    return hashlib.sha256(cfg.get('token','').encode()).hexdigest()


async def api(cfg, method, path, body=None, params=None):
    context = ssl.create_default_context()
    if config.MAX_CA_BUNDLE:
        context.load_verify_locations(cafile=config.MAX_CA_BUNDLE)
    async with httpx.AsyncClient(timeout=15,verify=context,trust_env=False,follow_redirects=False) as client:
        response = await client.request(method,API+path,headers={'Authorization':cfg['token']},json=body,params=params)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data,dict) or data.get('success') is False:
            raise ValueError('MAX не подтвердил операцию')
        return data


def navigation(cfg, info=False):
    origin = public_origin(cfg.get('public_url'))
    url = origin+'/mini/max'
    username = cfg.get('bot_username','')
    if cfg.get('mini_app') and re.fullmatch(r'[A-Za-z0-9_]{1,80}',username):
        url = f'https://max.ru/{username}?startapp'
    s = db.settings()
    text = f'{s["shop_name"]}\nВыберите блюда в меню с фотографиями. Заказ оформляется в мини-приложении.'
    if info:
        text = f'{s["shop_name"]}\nАдрес: {s["address"]}\nТелефон: {s["phone"]}\nВремя работы: {s["hours"]}'
    return {'text':text,'attachments':[{'type':'inline_keyboard','payload':{'buttons':[
        [{'type':'link','text':'🍣 Открыть меню', 'url':url}],
        [{'type':'callback','text':'📍 О магазине' if not info else '← Назад','payload':'info' if not info else 'menu'}],
        [{'type':'link','text':'Доставка и оплата','url':origin+'/legal/delivery'}]
    ]}}]}


def event_data(event):
    """Retain only routing data, not user names, phone numbers or message text."""
    kind = event.get('update_type')
    if kind not in EVENT_TYPES:
        return None
    stamp = event.get('timestamp')
    if type(stamp) is not int:
        raise ValueError()
    if kind=='bot_started':
        chat = event.get('chat_id')
        identity = str(stamp)
        action = 'menu'
        callback = ''
    else:
        message = event.get('message') or {}
        recipient = message.get('recipient') or {}
        if recipient.get('chat_type')!='dialog':
            return None
        chat = recipient.get('chat_id')
        callback_data = event.get('callback') or {}
        if kind=='message_created' and (message.get('sender') or {}).get('is_bot'):
            return None
        callback = str(callback_data.get('callback_id') or '') if kind=='message_callback' else ''
        identity = callback or str((message.get('body') or {}).get('mid') or '')
        action = 'info' if callback_data.get('payload')=='info' else 'menu'
    if type(chat) is not int or not -(2**63)<chat<2**63 or not identity or len(identity)>200:
        raise ValueError()
    if kind=='message_callback' and not callback:
        raise ValueError()
    return {'kind':kind,'chat_id':chat,'identity':identity,'callback_id':callback,'action':action}


@router.post('/api/max/webhook')
async def webhook(request: Request):
    cfg = get_config('max')
    expected = cfg.get('webhook_secret','')
    supplied = request.headers.get('X-Max-Bot-Api-Secret','')
    if not expected or not hmac.compare_digest(expected.encode(),supplied.encode()):
        raise HTTPException(403,'Неверная подпись webhook')
    if not cfg.get('enabled'):
        return {'ok':True}
    raw = await request.body()
    if len(raw)>128*1024:
        raise HTTPException(413,'Событие слишком большое')
    try:
        data = json.loads(raw)
        if not isinstance(data,dict):
            raise ValueError()
        event = event_data(data)
    except (ValueError,TypeError,AttributeError):
        raise HTTPException(400,'Некорректное событие') from None
    if event:
        key = bot_key(cfg)
        eid = hashlib.sha256(f"{key}:{event['kind']}:{event['chat_id']}:{event['identity']}".encode()).hexdigest()
        with db.connect(True) as c:
            c.execute('INSERT OR IGNORE INTO max_events(id,bot_key,payload,created) VALUES (?,?,?,?)',
                      (eid,key,json.dumps(event),time.time()))
    return {'ok':True}


async def process_event(cfg, event):
    body = navigation(cfg,event['action']=='info')
    if event['callback_id']:
        await api(cfg,'POST','/answers',{'message':body},{'callback_id':event['callback_id']})
        return
    key, chat = bot_key(cfg),event['chat_id']
    with db.connect() as c:
        previous = c.execute('SELECT message_id FROM max_screens WHERE bot_key=? AND chat_id=?',(key,chat)).fetchone()
    if previous:
        try:
            await api(cfg,'PUT','/messages',body,{'message_id':previous['message_id']})
            return
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code!=404:
                raise
    try:
        response = await api(cfg,'POST','/messages',body,{'chat_id':chat})
    except (httpx.ReadTimeout,httpx.WriteTimeout,httpx.ReadError,httpx.RemoteProtocolError):
        raise DeliveryUnknown() from None
    mid = str(((response.get('message') or {}).get('body') or {}).get('mid') or '')
    if not mid:
        raise DeliveryUnknown('MAX не вернул идентификатор сообщения')
    with db.connect(True) as c:
        c.execute('INSERT INTO max_screens VALUES (?,?,?) ON CONFLICT(bot_key,chat_id) DO UPDATE SET message_id=excluded.message_id',(key,chat,mid))


async def tick():
    cfg = get_config('max')
    if not cfg.get('enabled') or not cfg.get('token'):
        return
    now = time.time()
    with db.connect(True) as c:
        c.execute("UPDATE max_events SET state='failed',error='Исчерпаны попытки обработки события' WHERE state='pending' AND attempts>=5 AND next_try<=?",(now,))
        c.execute("UPDATE max_events SET state='failed',error='Токен MAX изменился; старое событие пропущено' WHERE bot_key<>? AND state='pending'",(bot_key(cfg),))
        c.execute("DELETE FROM max_events WHERE state IN ('done','failed') AND created<?",(now-7*86400,))
        row = c.execute("SELECT * FROM max_events WHERE bot_key=? AND state='pending' AND attempts<5 AND next_try<=? ORDER BY created LIMIT 1",(bot_key(cfg),now)).fetchone()
        if not row:
            return
        c.execute('UPDATE max_events SET next_try=?,attempts=attempts+1 WHERE id=?',(now+60,row['id']))
    try:
        await process_event(cfg,json.loads(row['payload']))
        with db.connect(True) as c:
            c.execute("UPDATE max_events SET state='done',error='',payload='{}' WHERE id=?",(row['id'],))
    except Exception as exc:
        status = exc.response.status_code if isinstance(exc,httpx.HTTPStatusError) else None
        # No response bodies/headers/token in logs or admin HTML.
        error = f'MAX HTTP {status}' if status else 'MAX: '+type(exc).__name__
        if isinstance(exc,DeliveryUnknown):
            error = 'Результат отправки неизвестен. Автоповтор остановлен, чтобы не дублировать сообщение.'
        state = 'failed' if isinstance(exc,DeliveryUnknown) or row['attempts']>=4 or status in (400,401,403,404) else 'pending'
        with db.connect(True) as c:
            c.execute('UPDATE max_events SET state=?,error=?,next_try=? WHERE id=?',(state,error,time.time()+min(600,30*2**row['attempts']),row['id']))


async def worker():
    while True:
        try:
            await tick()
        except Exception as exc:
            log.warning('MAX worker: %s',type(exc).__name__)
        await asyncio.sleep(1)


@router.post('/admin/integrations/max/{action}')
async def configure(request: Request, action: str):
    from .admin import form_data, redirect
    await form_data(request)
    cfg = get_config('max')
    original = dict(cfg)
    try:
        if action not in ('check','subscribe'):
            raise ValueError('Неизвестное действие')
        if not cfg.get('token'):
            raise ValueError('Сначала сохраните токен MAX')
        me = await api(cfg,'GET','/me')
        username = str(me.get('username') or '')
        if not re.fullmatch(r'[A-Za-z0-9_]{1,80}',username):
            raise ValueError('MAX не вернул имя бота')
        if action=='subscribe':
            origin = public_origin(cfg.get('public_url'))
            if not origin or not cfg.get('enabled'):
                raise ValueError('Сохраните HTTPS-адрес магазина и включите MAX')
            cfg.setdefault('webhook_secret',secrets.token_hex(32))
            await api(cfg,'POST','/subscriptions',{'url':origin+'/api/max/webhook','update_types':EVENT_TYPES,'secret':cfg['webhook_secret']})
            cfg['subscribed'] = True
        cfg['bot_username'] = username
        if get_config('max')!=original:
            raise ValueError('Настройки MAX изменились. Повторите проверку.')
        set_config('max',cfg)
        return redirect('/admin/integrations',ok='MAX: подключён @'+username+('. Webhook зарегистрирован.' if action=='subscribe' else '. Доступ подтверждён.'))
    except ValueError as exc:
        return redirect('/admin/integrations',error=exc)
    except Exception:
        return redirect('/admin/integrations',error='MAX не подтвердил подключение. Проверьте токен, HTTPS и доверенные сертификаты сервера (MAX_CA_BUNDLE).')
