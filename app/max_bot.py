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

from . import config, db, max_chat, customer_accounts as accounts
from .mini_apps import public_origin
from .vault import get_config, set_config, seal, unseal

router = APIRouter()
log = logging.getLogger(__name__)
API = 'https://platform-api2.max.ru'
EVENT_TYPES = ['bot_started','message_created','message_callback']


class DeliveryUnknown(Exception):
    """A send may have succeeded; do not duplicate it automatically."""


def connection_error(exc):
    """Allowlisted diagnostics only: never render provider bodies, tokens or headers."""
    cause, seen = exc, set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause,ssl.SSLCertVerificationError) or 'CERTIFICATE_VERIFY_FAILED' in str(cause):
            return 'MAX: сервер не доверяет цепочке TLS-сертификатов API. Настройте MAX_CA_BUNDLE с официальным сертификатом Минцифры и перезапустите sushi-web.'
        cause=cause.__cause__ or cause.__context__
    if isinstance(exc,FileNotFoundError):
        return 'MAX: файл MAX_CA_BUNDLE не найден. Проверьте абсолютный путь в .env.'
    if isinstance(exc,PermissionError):
        return 'MAX: пользователь sushi не может прочитать MAX_CA_BUNDLE.'
    if isinstance(exc,ssl.SSLError):
        return 'MAX: ошибка TLS или формата PEM в MAX_CA_BUNDLE.'
    if isinstance(exc,httpx.HTTPStatusError):
        status=exc.response.status_code
        reason={401:'Токен не принят. Проверьте сохранённый токен бота.',
                403:'Доступ запрещён. Проверьте токен и доступ бота к API.',
                400:'Параметры запроса отклонены. Проверьте HTTPS-адрес webhook.',
                429:'Превышена частота запросов. Повторите позже.'}.get(status,'API вернул ошибку. Повторите позже.')
        return f'MAX HTTP {status}: {reason}'
    if isinstance(exc,httpx.TimeoutException):
        return 'MAX: время ожидания истекло. Проверьте исходящий HTTPS-доступ VPS к platform-api2.max.ru:443.'
    if isinstance(exc,httpx.ConnectError):
        return 'MAX: соединение не установлено. Проверьте DNS, исходящий порт 443 и цепочку TLS-сертификатов.'
    return 'MAX: ответ не подтверждён. Проверьте соединение командой python -m app.max_check.'


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
    menu_button = {'type':'link','text':'🍣 Меню с фото','url':origin+'/mini/max'}
    username = cfg.get('bot_username','')
    if cfg.get('mini_app') and re.fullmatch(r'[A-Za-z0-9_]{1,80}',username):
        menu_button = {'type':'open_app','text':'🍣 Меню с фото','web_app':username}
    s = db.settings()
    text = f'🍣 {s["shop_name"]}\nРоллы для уютного вечера, встречи с друзьями или вкусного обеда.\n\nВыбирайте доставку или самовывоз. История и статусы — в «Мои заказы».'
    if info:
        text = f'{s["shop_name"]}\nАдрес: {s["address"]}\nТелефон: {s["phone"]}\nВремя работы: {s["hours"]}'
    return {'text':text,'attachments':[{'type':'inline_keyboard','payload':{'buttons':[
        [max_chat.button('🍣 Меню в чате','menu'),max_chat.button('🛒 Корзина','cart')],
        [menu_button],
        [{'type':'callback','text':'👤 Личный кабинет','payload':'account'},
         {'type':'callback','text':'📦 Мои заказы','payload':'orders'}],
        [{'type':'callback','text':'📍 О магазине' if not info else '← Назад','payload':'info' if not info else 'menu'}],
        [{'type':'link','text':'Доставка и оплата','url':origin+'/legal/delivery'}]
    ]}}]}


def event_data(event, cfg=None):
    """Store bounded checkout input encrypted; contacts require a valid signature."""
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
        user = (event.get('user') or {}).get('user_id')
        if event.get('payload')=='account':
            action='account'
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
        action = 'text' if kind=='message_created' else 'menu'
        user = (callback_data.get('user') or {}).get('user_id') if kind=='message_callback' else (message.get('sender') or {}).get('user_id')
        raw_text = (message.get('body') or {}).get('text') or ''
        if not isinstance(raw_text,str):
            raise ValueError()
        value = callback_data.get('payload') if kind=='message_callback' else raw_text.strip().removeprefix('/')
        commands = ('start','menu','cart','privacy','info','account','orders','stopupdates','logout','cancel','updates_on')
        if kind=='message_callback':
            if not isinstance(value,str) or not (value in commands or re.fullmatch(r'orders:[0-9]{1,18}',value) or (value!='text' and max_chat.handles(value))):
                return None
            action='menu' if value=='start' else value
        elif raw_text.strip().startswith('/') and value in commands:
            action='menu' if value=='start' else value
        if kind=='message_created' and any(a.get('type')=='contact' for a in (message.get('body') or {}).get('attachments') or []):
            action='contact'
    if type(chat) is not int or not -(2**63)<chat<2**63 or not identity or len(identity)>200:
        raise ValueError()
    if kind=='message_callback' and not callback:
        raise ValueError()
    result = {'kind':kind,'chat_id':chat,'identity':identity,'callback_id':callback,'action':action}
    if kind=='message_callback':
        result['message_id']=str((message.get('body') or {}).get('mid') or '')
    if type(user) is int and 0<user<2**63:
        result['user_id']=user
        if action=='text':
            # Overlong text is rejected by checkout validation, never silently truncated.
            result['text']=seal(raw_text.strip() if len(raw_text.strip())<=500 else '')
        if action=='contact' and cfg:
            phone=accounts.max_contact_phone(event,cfg['token'])
            if phone:
                result['contact']=seal(phone)
    elif action not in ('menu','info'):
        return None
    return result


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
        event = event_data(data,cfg)
    except (ValueError,TypeError,AttributeError):
        raise HTTPException(400,'Некорректное событие') from None
    if event:
        key = bot_key(cfg)
        eid = hashlib.sha256(f"{key}:{event['kind']}:{event['chat_id']}:{event['identity']}".encode()).hexdigest()
        with db.connect(True) as c:
            c.execute('INSERT OR IGNORE INTO max_events(id,bot_key,payload,created) VALUES (?,?,?,?)',
                      (eid,key,json.dumps(event),time.time()))
    return {'ok':True}


async def navigation_api(cfg, method, path, body, params):
    """An unavailable product image must not block the ordering keyboard."""
    try:
        return await api(cfg,method,path,body,params)
    except httpx.HTTPStatusError as exc:
        message=body.get('message',body)
        attachments=message.get('attachments',[])
        if exc.response.status_code!=400 or not any(a.get('type')=='image' for a in attachments):
            raise
        fallback=dict(message,attachments=[a for a in attachments if a.get('type')!='image'])
        fallback['text']+='\nФото временно недоступно.'
        return await api(cfg,method,path,{'message':fallback} if 'message' in body else fallback,params)


async def process_event(cfg, event):
    body = navigation(cfg,event['action']=='info')
    action,user,chat,key = event['action'],event.get('user_id'),event['chat_id'],bot_key(cfg)
    persistent = False
    account = accounts.resume('max',key,user,chat,enable=action=='updates_on') if action in ('account','updates_on') else None
    if action=='updates_on':
        action='account'
        if account:
            max_chat.notifications(key,user,True)
    if account:
        action = 'orders'
    if action=='account':
        max_chat.reset(key,user)
        accounts.begin_contact('max',key,user,chat)
        body={'text':accounts.CONTACT_PROMPT,'attachments':[{'type':'inline_keyboard','payload':{'buttons':[
            [{'type':'request_contact','text':'Поделиться своим номером'}],
            [{'type':'callback','text':'Отмена','payload':'cancel'}]]}}]}
    elif action=='contact' and accounts.waiting_contact('max',key,user,chat):
        try:
            phone=unseal(event['contact']) if event.get('contact') else ''
            accounts.verify_contact('max',key,user,chat,phone)
            max_chat.notifications(key,user,True)
            body['text']='Номер подтверждён. Уведомления включены. Нажмите «Мои заказы»: здесь будут и новые заказы с этим телефоном.'
        except ValueError as exc:
            body['text']=str(exc)
    elif action.startswith('orders'):
        max_chat.reset(key,user)
        accounts.cancel_contact('max',key,user)
        before=int(action.split(':')[1]) if ':' in action else 0
        texts,cursor,linked=accounts.history('max',key,user,before,compact=True)
        if texts:
            body['text']='\n\n'.join(texts)+'\n\nИстория сохранена в чате. /orders — актуальные статусы.'
            if cursor:
                body['attachments'][0]['payload']['buttons'].insert(0,[{'type':'callback','text':'Ещё заказы →','payload':f'orders:{cursor}'}])
            persistent=True
        else:
            body['text']='Заказов пока нет.' if linked else 'Для заказов с сайта подтвердите свой номер: /account.'
        if account:
            body['text'] = ('С возвращением! Статусы новых заказов с вашим номером придут автоматически.\n\n'
                            if account['notifications'] else 'С возвращением! Уведомления отключены.\n\n') + body['text']
            if not account['notifications']:
                body['attachments'][0]['payload']['buttons'].insert(0,[max_chat.button('Включить уведомления','updates_on')])
    elif action in ('stopupdates','logout'):
        accounts.stop('max',key,user,logout=action=='logout')
        max_chat.reset(key,user)
        max_chat.notifications(key,user,False)
        body['text']='Вы вышли. Заказы и сообщения в чате сохранены.' if action=='logout' else 'Уведомления отключены. История: /orders. Включить снова: /account.'
    elif action=='text' and accounts.waiting_contact('max',key,user,chat):
        body['text']='Для входа нужен собственный контакт кнопкой. Ввод номера текстом не открывает историю. /account — запросить кнопку, /cancel — отменить.'
    elif max_chat.handles(action) or action=='contact':
        if user:
            accounts.cancel_contact('max',key,user)
        body=max_chat.handle(cfg,event)
    elif user:
        accounts.cancel_contact('max',key,user)
        max_chat.reset(key,user)
    if persistent:
        # History messages must never be edited/deleted by navigation callbacks.
        if event['callback_id']:
            await api(cfg,'POST','/answers',{'notification':'История заказов'},{'callback_id':event['callback_id']})
        await send_persistent(cfg,chat,body)
        return
    if event['callback_id']:
        with db.connect() as c:
            tracked=c.execute('SELECT message_id FROM max_screens WHERE bot_key=? AND chat_id=?',(key,chat)).fetchone()
        if tracked and event.get('message_id')==tracked['message_id']:
            await navigation_api(cfg,'POST','/answers',{'message':body},{'callback_id':event['callback_id']})
            return
        await api(cfg,'POST','/answers',{'notification':'Готово'},{'callback_id':event['callback_id']})
    key, chat = bot_key(cfg),event['chat_id']
    with db.connect() as c:
        previous = c.execute('SELECT message_id FROM max_screens WHERE bot_key=? AND chat_id=?',(key,chat)).fetchone()
    if previous:
        try:
            await navigation_api(cfg,'PUT','/messages',body,{'message_id':previous['message_id']})
            return
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code!=404:
                raise
    try:
        response = await navigation_api(cfg,'POST','/messages',body,{'chat_id':chat})
    except (httpx.ReadTimeout,httpx.WriteTimeout,httpx.ReadError,httpx.RemoteProtocolError):
        raise DeliveryUnknown() from None
    mid = str(((response.get('message') or {}).get('body') or {}).get('mid') or '')
    if not mid:
        raise DeliveryUnknown('MAX не вернул идентификатор сообщения')
    with db.connect(True) as c:
        c.execute('INSERT INTO max_screens VALUES (?,?,?) ON CONFLICT(bot_key,chat_id) DO UPDATE SET message_id=excluded.message_id',(key,chat,mid))


async def send_persistent(cfg, chat, body):
    try:
        response=await api(cfg,'POST','/messages',body,{'chat_id':chat})
    except (httpx.ReadTimeout,httpx.WriteTimeout,httpx.ReadError,httpx.RemoteProtocolError):
        raise DeliveryUnknown() from None
    if not ((response.get('message') or {}).get('body') or {}).get('mid'):
        raise DeliveryUnknown()


async def deliver_customer_once(cfg, native=False):
    row=max_chat.claim(bot_key(cfg)) if native else accounts.claim('max',bot_key(cfg))
    if not row:
        return
    state,error,delay=1,'',30
    try:
        await send_persistent(cfg,row['chat_id'],{'text':row['text']})
    except DeliveryUnknown:
        state,error=-1,'Результат отправки неизвестен. Проверьте историю чата перед повтором.'
    except Exception as exc:
        status=exc.response.status_code if isinstance(exc,httpx.HTTPStatusError) else None
        state=-1 if status in (400,401,403,404) else 0
        error=connection_error(exc)
        delay=min(600,30*2**row['attempts'])
    (max_chat.complete if native else accounts.complete)(row['id'],state,error,delay)
    return True


async def tick():
    cfg = get_config('max')
    if not cfg.get('enabled') or not cfg.get('token'):
        return
    # At most one outbound message per tick, leaving headroom for callbacks.
    if await deliver_customer_once(cfg,native=True) or await deliver_customer_once(cfg):
        return
    now = time.time()
    with db.connect(True) as c:
        c.execute("UPDATE max_events SET state='failed',error='Исчерпаны попытки обработки события' WHERE state='pending' AND attempts>=5 AND next_try<=?",(now,))
        c.execute("UPDATE max_events SET state='failed',error='Токен MAX изменился; старое событие пропущено' WHERE bot_key<>? AND state='pending'",(bot_key(cfg),))
        c.execute("DELETE FROM max_events WHERE state IN ('done','failed') AND created<?",(now-7*86400,))
        row = c.execute("""SELECT e.* FROM max_events e WHERE bot_key=? AND state='pending' AND attempts<5 AND next_try<=?
            AND NOT EXISTS(SELECT 1 FROM max_events older WHERE older.bot_key=e.bot_key AND older.state='pending'
                AND json_extract(older.payload,'$.chat_id')=json_extract(e.payload,'$.chat_id')
                AND (older.created<e.created OR (older.created=e.created AND older.rowid<e.rowid)))
            ORDER BY created,rowid LIMIT 1""",(bot_key(cfg),now)).fetchone()
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
    except Exception as exc:
        return redirect('/admin/integrations',error=connection_error(exc))
