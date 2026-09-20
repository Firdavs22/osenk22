import hashlib
import hmac
import re
import secrets
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from . import config, db

router = APIRouter()


def visitor(request):
    if 'visitor' not in request.session:
        # Telegram user IDs are positive. Anonymous web IDs occupy a separate namespace.
        request.session['visitor'] = -secrets.randbelow(2**62) - 1
    return request.session['visitor']


def limit(request, action, maximum):
    key = hashlib.sha256((action + ':' + (request.client.host if request.client else 'unknown')).encode()).hexdigest()
    now = time.time()
    with db.connect(True) as c:
        c.execute('DELETE FROM web_limits WHERE until<?', (now,))
        c.execute('INSERT INTO web_limits VALUES (?,1,?) ON CONFLICT(key) DO UPDATE SET count=count+1', (key, now+600))
        count = c.execute('SELECT count FROM web_limits WHERE key=?', (key,)).fetchone()[0]
    if count > maximum:
        raise HTTPException(429, 'Слишком много запросов. Попробуйте через несколько минут.')


async def payload(request):
    expected = request.session.get('csrf', '')
    if not expected or not hmac.compare_digest(request.headers.get('x-csrf-token', '').encode(), expected.encode()):
        raise HTTPException(403, 'Обновите страницу и повторите действие')
    limit(request, 'cart', 300)
    try:
        value = await request.json()
    except ValueError:
        raise HTTPException(400, 'Некорректный запрос')
    if not isinstance(value, dict):
        raise HTTPException(400, 'Некорректный запрос')
    return value


def cart_state(user):
    items = [dict(p) for p in db.cart(user)]
    return {'items': items, 'subtotal': sum(p['price']*p['quantity'] for p in items),
            'quantity': sum(p['quantity'] for p in items)}


@router.get('/')
def storefront(request: Request):
    from .admin import render
    from .integrations import payment_enabled
    visitor(request)
    with db.connect() as c:
        slides = c.execute('SELECT * FROM slides WHERE active=1 ORDER BY position,id').fetchall()
    products = [dict(p) for p in db.products(active=True)]
    # Only customer-facing catalog fields cross the public boundary.
    public = [{k: p[k] for k in ('id', 'name', 'description', 'ingredients', 'weight', 'price', 'photo', 'category_id', 'tags')} for p in products]
    return render(request, 'store.html', products=public, categories=db.categories(), slides=slides,
                  tags=list(dict.fromkeys(t.strip() for p in products for t in p['tags'].split(',') if t.strip())),
                  online=payment_enabled(), cart=cart_state(visitor(request)))


@router.get('/assets/{filename}')
def public_media(filename: str):
    if not re.fullmatch(r'[a-f0-9]{32}\.jpg', filename) or Path(filename).name != filename:
        raise HTTPException(404)
    with db.connect() as c:
        allowed = (c.execute('SELECT 1 FROM products WHERE photo=? AND active=1', (filename,)).fetchone()
                   or c.execute('SELECT 1 FROM slides WHERE photo=? AND active=1', (filename,)).fetchone()
                   or db.settings(c).get('logo') == filename)
    if not allowed or not (config.MEDIA / filename).is_file():
        raise HTTPException(404)
    return FileResponse(config.MEDIA / filename, media_type='image/jpeg')


@router.get('/api/cart')
def get_cart(request: Request):
    # Do not disclose backend product mappings through the cart endpoint.
    state = cart_state(visitor(request))
    state['items'] = [{k: p[k] for k in ('id', 'name', 'price', 'quantity', 'photo', 'active')} for p in state['items']]
    return state


@router.post('/api/cart')
async def change_cart(request: Request):
    data = await payload(request)
    try:
        pid, delta = data.get('id'), data.get('delta')
        if type(pid) is not int or type(delta) is not int or delta not in (-1, 1, -99):
            raise ValueError('Некорректное количество')
        db.cart_change(visitor(request), pid, delta)
        return get_cart(request)
    except ValueError as exc:
        return JSONResponse({'detail': str(exc)}, 400)


@router.post('/api/quote')
async def quote_cart(request: Request):
    data = await payload(request)
    try:
        user = visitor(request)
        q = db.quote(user, data)
        # An opaque checkout key identifies one purchase, even across retries/network loss.
        key = request.session.setdefault('checkout_key', secrets.token_urlsafe(24))
        return {k: v for k, v in q.items() if k != 'items'} | {'key': key}
    except ValueError as exc:
        return JSONResponse({'detail': str(exc)}, 400)


def contact(data, name, minimum, maximum):
    value = str(data.get(name, '')).strip()
    if not minimum <= len(value) <= maximum:
        raise ValueError(f'Заполните поле «{name}» ({minimum}–{maximum} символов)')
    return value


@router.post('/api/checkout')
async def checkout(request: Request):
    from .integrations import create_payment, payment_enabled
    data = await payload(request)
    user = visitor(request)
    key = str(data.get('key', ''))
    try:
        # Look up a completed attempt before reading a cart that might already be empty.
        with db.connect() as c:
            old = c.execute('SELECT public_token FROM orders WHERE token=? AND user_id=?', (key, user)).fetchone()
        if old:
            return {'url': '/order/' + old['public_token']}
        if not key or key != request.session.get('checkout_key'):
            raise ValueError('Обновите итог заказа')
        limit(request, 'checkout', 10)
        customer = contact(data, 'customer', 2, 80)
        phone = re.sub(r'[\s()\-]', '', contact(data, 'phone', 10, 30))
        if re.fullmatch(r'8\d{10}', phone):
            phone = '+7' + phone[1:]
        if not re.fullmatch(r'\+\d{10,15}', phone):
            raise ValueError('Введите телефон с кодом страны, например +79991234567')
        comment = contact(data, 'comment', 0, 500)
        if data.get('consent') is not True:
            raise ValueError('Подтвердите согласие с условиями заказа')
        method = data.get('method')
        payment = data.get('payment', 'cash')
        if payment not in ('cash', 'tbank') or (payment == 'tbank' and not payment_enabled()):
            raise ValueError('Выбранный способ оплаты недоступен')
        address = contact(data, 'address', 10, 400) if method == 'delivery' else db.settings()['address']
        with db.connect(True) as c:
            # Transaction serializes concurrent duplicate submissions and cart changes.
            old = c.execute('SELECT public_token FROM orders WHERE token=? AND user_id=?', (key, user)).fetchone()
            if old:
                return {'url': '/order/' + old['public_token']}
            q = db.quote(user, data, c)
            if q['fingerprint'] != data.get('fingerprint'):
                raise ValueError('Цена или корзина изменились. Проверьте новый итог и подтвердите заказ ещё раз.')
            public_token = secrets.token_urlsafe(32)
            oid = c.execute("""INSERT INTO orders(token,user_id,customer,phone,method,address,comment,subtotal,delivery,total,currency,channel,payment_method,payment_status,public_token,consent_at,notified)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,'web',?,?,?,strftime('%Y-%m-%d %H:%M:%S','now'),0)""",
                (key,user,customer,phone,method,address,comment,q['subtotal'],q['delivery'],q['total'],q['currency'],payment,'pending' if payment=='tbank' else 'unpaid',public_token)).lastrowid
            c.executemany('INSERT INTO order_items(order_id,name,price,quantity,product_id,iiko_id,iiko_size) VALUES (?,?,?,?,?,?,?)',
                          [(oid,p['name'],p['price'],p['quantity'],p['id'],p['iiko_id'],p['iiko_size']) for p in q['items']])
            if payment == 'tbank':
                create_payment(c, oid)
            else:
                notify_order(c, oid)
            c.execute('DELETE FROM cart WHERE user_id=?', (user,))
        request.session.pop('checkout_key', None)
        return {'url': '/order/' + public_token}
    except ValueError as exc:
        return JSONResponse({'detail': str(exc)}, 400)


def notify_order(c, oid):
    order = c.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
    if order['notified']:
        return
    items = c.execute('SELECT * FROM order_items WHERE order_id=?', (oid,)).fetchall()
    summary = db.order_summary(order, items)
    for admin in db.admin_ids():
        db.enqueue(c, admin, 'Новый заказ с сайта!\n\n' + summary)
    c.execute('UPDATE orders SET notified=1 WHERE id=?', (oid,))


@router.get('/order/{token}')
def order_result(request: Request, token: str):
    from .admin import render
    with db.connect() as c:
        order = c.execute('SELECT * FROM orders WHERE public_token=?', (token,)).fetchone()
        if not order:
            raise HTTPException(404)
        payment = c.execute('SELECT state,url,error FROM payments WHERE order_id=?', (order['id'],)).fetchone()
        items = c.execute('SELECT * FROM order_items WHERE order_id=?', (order['id'],)).fetchall()
    return render(request, 'store_order.html', order=order, payment=payment, items=items)


@router.get('/legal/{page}')
def legal(request: Request, page: str):
    from .admin import render
    if page not in ('privacy', 'offer'):
        raise HTTPException(404)
    return render(request, 'legal.html', legal_page=page)
