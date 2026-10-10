"""Explicit Telegram subscriptions for guest orders; no trust in Mini App launch data."""
import hashlib
import re
import secrets
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from . import config, db
from .vault import get_config

router = APIRouter()


def token_fingerprint(token):
    return hashlib.sha256(token.encode()).hexdigest()


def telegram_state(c=None):
    if c is None:
        with db.connect() as conn:
            return telegram_state(conn)
    cfg = get_config('telegram', c)
    token = cfg.get('token') or config.BOT_TOKEN
    enabled = cfg.get('enabled', True)
    ids = cfg.get('admin_ids', config.ADMIN_IDS) if enabled else []
    row = c.execute('SELECT * FROM telegram_runtime WHERE id=1').fetchone()
    current = bool(token and row and row['fingerprint']==token_fingerprint(token))
    return {'enabled':enabled,'token_set':bool(token),'admin_ids':ids,
            'username':row['username'] if current else '', 'bot_id':row['bot_id'] if current else 0,
            'alive':bool(enabled and current and time.time()-row['heartbeat']<120)}


def record_bot(bot_id, username, token, heartbeat=False):
    if not username or not re.fullmatch(r'[A-Za-z0-9_]{5,64}', username):
        return
    stamp = time.time() if heartbeat else 0
    with db.connect(True) as c:
        c.execute('''INSERT INTO telegram_runtime VALUES (1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            bot_id=excluded.bot_id,username=excluded.username,
            heartbeat=CASE WHEN excluded.heartbeat>0 THEN excluded.heartbeat
                WHEN telegram_runtime.fingerprint=excluded.fingerprint THEN telegram_runtime.heartbeat ELSE 0 END,
            fingerprint=excluded.fingerprint''',(bot_id,username,token_fingerprint(token),stamp))


def status_message(order):
    return f'Заказ №{order["id"]}: {db.STATUSES[order["status"]]}. Для отключения уведомлений: /stopupdates'


def queue_status(c, order):
    sub = c.execute('SELECT * FROM order_subscriptions WHERE order_id=?',(order['id'],)).fetchone()
    if sub:
        c.execute('INSERT INTO outbox(chat_id,text,subscription_order,subscription_bot) VALUES (?,?,?,?)',
                  (sub['chat_id'],status_message(order),order['id'],sub['bot_id']))


def subscribe(ticket, chat_id, bot_id):
    if not re.fullmatch(r'[A-Za-z0-9_-]{43}',ticket):
        raise ValueError('Ссылка недействительна. Получите новую на странице заказа.')
    with db.connect(True) as c:
        row = c.execute('SELECT * FROM order_subscription_tickets WHERE token_hash=?',
                        (token_fingerprint(ticket),)).fetchone()
        if not row or row['expires']<time.time() or row['bot_id']!=bot_id:
            raise ValueError('Ссылка истекла или уже использована. Получите новую на странице заказа.')
        sub = c.execute('SELECT * FROM order_subscriptions WHERE order_id=?',(row['order_id'],)).fetchone()
        if sub and (sub['chat_id']!=chat_id or sub['bot_id']!=bot_id):
            raise ValueError('Для этого заказа уже подключены уведомления. Отключите их на странице заказа перед новой подпиской.')
        c.execute('INSERT OR IGNORE INTO order_subscriptions VALUES (?,?,?)',(row['order_id'],chat_id,bot_id))
        c.execute('DELETE FROM order_subscription_tickets WHERE order_id=?',(row['order_id'],))
        order = c.execute('SELECT * FROM orders WHERE id=?',(row['order_id'],)).fetchone()
        return 'Уведомления подключены. '+status_message(order)


def unsubscribe_chat(chat_id, bot_id):
    with db.connect(True) as c:
        c.execute('DELETE FROM order_subscriptions WHERE chat_id=? AND bot_id=?',(chat_id,bot_id))
        c.execute('DELETE FROM outbox WHERE chat_id=? AND subscription_bot=? AND subscription_order IS NOT NULL AND sent!=1',(chat_id,bot_id))


@router.post('/order/{token}/telegram')
async def link_telegram(request: Request, token: str):
    from .admin import form_data, render
    from .store import limit
    form = await form_data(request, admin=False)
    try:
        limit(request,'subscribe',20)
        with db.connect(True) as c:
            order = c.execute('SELECT id FROM orders WHERE public_token=?',(token,)).fetchone()
            if not order:
                raise HTTPException(404)
            if form.get('action')=='stop':
                c.execute('DELETE FROM order_subscriptions WHERE order_id=?',(order['id'],))
                c.execute('DELETE FROM order_subscription_tickets WHERE order_id=?',(order['id'],))
                c.execute('DELETE FROM outbox WHERE subscription_order=? AND sent!=1',(order['id'],))
                return RedirectResponse('/order/'+token,status_code=303)
            state = telegram_state(c)
            if not state['enabled'] or not state['username']:
                raise HTTPException(503,'Бот пока не подключён. Следите за статусом на странице заказа.')
            ticket = secrets.token_urlsafe(32)
            c.execute('DELETE FROM order_subscription_tickets WHERE expires<? OR order_id=?',(time.time(),order['id']))
            c.execute('INSERT INTO order_subscription_tickets VALUES (?,?,?,?)',
                      (token_fingerprint(ticket),order['id'],state['bot_id'],time.time()+600))
        # Keep POST on this origin: CSP form-action 'self' can block cross-origin redirects.
        return render(request,'order_telegram.html',order_token=token,
                      telegram_link=f'https://t.me/{state["username"]}?start=watch_{ticket}')
    finally:
        await form.close()


@router.get('/api/order/{token}/status')
def public_status(token: str):
    with db.connect() as c:
        order = c.execute('SELECT status,payment_status FROM orders WHERE public_token=?',(token,)).fetchone()
        if not order:
            raise HTTPException(404)
        payment = c.execute('SELECT p.state,p.url FROM payments p JOIN orders o ON o.id=p.order_id WHERE o.public_token=?',(token,)).fetchone()
        subscribed = bool(c.execute('SELECT 1 FROM order_subscriptions s JOIN orders o ON o.id=s.order_id WHERE o.public_token=?',(token,)).fetchone())
    # No customer details, phone/address or provider URL are returned by polling.
    return {'status':order['status'],'payment_status':order['payment_status'],
            'payment_state':payment['state'] if payment else '', 'payment_ready':bool(payment and payment['url']), 'subscribed':subscribed}
