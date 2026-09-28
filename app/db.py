import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation

from . import config

STATUSES = {'new': 'Новый', 'accepted': 'Принят', 'cooking': 'Готовится',
            'ready': 'Готов / передан курьеру', 'done': 'Выполнен', 'cancelled': 'Отменён'}
TRANSITIONS = {'new': ['accepted', 'cancelled'], 'accepted': ['cooking', 'cancelled'],
               'cooking': ['ready', 'cancelled'], 'ready': ['done', 'cancelled'],
               'done': [], 'cancelled': []}
DEFAULTS = {'shop_name': config.SHOP_NAME, 'currency': config.CURRENCY,
            'phone': '', 'address': '', 'hours': 'Уточните время работы у администратора',
            'delivery_fee': '20000', 'free_delivery_from': '200000',
            'minimum_order': '50000', 'orders_open': '0', 'delivery_enabled': '1'}


@contextmanager
def connect(write=False):
    config.prepare()
    conn = sqlite3.connect(config.DB, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA busy_timeout=15000')
    try:
        if write:
            conn.execute('BEGIN IMMEDIATE')
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init():
    with connect() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS categories(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
        CREATE TABLE IF NOT EXISTS products(
          id INTEGER PRIMARY KEY, category_id INTEGER NOT NULL REFERENCES categories(id),
          name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', ingredients TEXT NOT NULL,
          weight TEXT NOT NULL DEFAULT '', price INTEGER NOT NULL CHECK(price>0),
          photo TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS cart(
          user_id INTEGER NOT NULL, product_id INTEGER NOT NULL REFERENCES products(id),
          quantity INTEGER NOT NULL CHECK(quantity BETWEEN 1 AND 99),
          PRIMARY KEY(user_id,product_id));
        CREATE TABLE IF NOT EXISTS drafts(
          user_id INTEGER PRIMARY KEY, step TEXT NOT NULL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS orders(
          id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT NOT NULL UNIQUE, user_id INTEGER NOT NULL,
          customer TEXT NOT NULL, phone TEXT NOT NULL, method TEXT NOT NULL, address TEXT NOT NULL,
          comment TEXT NOT NULL, subtotal INTEGER NOT NULL, delivery INTEGER NOT NULL,
          total INTEGER NOT NULL, currency TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new',
          created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now')));
        CREATE INDEX IF NOT EXISTS orders_user ON orders(user_id,id);
        CREATE TABLE IF NOT EXISTS order_items(
          id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id),
          name TEXT NOT NULL, price INTEGER NOT NULL, quantity INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS outbox(
          id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, text TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0,
          sent INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS login_limits(
          ip TEXT PRIMARY KEY, attempts INTEGER NOT NULL, until REAL NOT NULL);
        ''')
        c.executemany('INSERT OR IGNORE INTO settings VALUES (?,?)', DEFAULTS.items())
        from .schema import migrate
        migrate(c)


def settings(c=None):
    if c is not None:
        return dict(c.execute('SELECT key,value FROM settings').fetchall())
    with connect() as conn:
        return settings(conn)


def money(value, currency=None):
    number = f'{int(value) / 100:,.2f}'.replace(',', ' ').replace('.00', '').replace('.', ',')
    return f'{number} {currency if currency is not None else settings()["currency"]}'


def parse_money(value, allow_zero=True):
    try:
        amount = Decimal(str(value).replace(',', '.').replace(' ', ''))
        if not amount.is_finite() or amount < 0 or amount > 1000000 or amount * 100 != (amount * 100).to_integral_value():
            raise ValueError()
        if not allow_zero and amount == 0:
            raise ValueError()
        return int(amount * 100)
    except (InvalidOperation, ValueError):
        raise ValueError('Сумма должна быть от 0 до 1 000 000, максимум 2 знака после запятой')


def categories():
    with connect() as c:
        return c.execute('SELECT * FROM categories ORDER BY id').fetchall()


def products(category=None, active=False):
    sql = 'SELECT p.*, c.name category FROM products p JOIN categories c ON c.id=p.category_id WHERE 1=1'
    args = []
    if category is not None:
        sql += ' AND category_id=?'
        args.append(category)
    if active:
        sql += ' AND active=1'
    with connect() as c:
        return c.execute(sql + ' ORDER BY p.id DESC', args).fetchall()


def product(pid):
    with connect() as c:
        return c.execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone()


def cart(user, c=None):
    if c is None:
        with connect() as conn:
            return cart(user, conn)
    return c.execute('SELECT p.*,cart.quantity FROM cart JOIN products p ON p.id=cart.product_id WHERE user_id=? ORDER BY p.id', (user,)).fetchall()


def cart_change(user, pid, delta):
    with connect(True) as c:
        p = c.execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone()
        if not p or (delta > 0 and not p['active']):
            raise ValueError('Товар сейчас недоступен')
        old = c.execute('SELECT quantity FROM cart WHERE user_id=? AND product_id=?', (user, pid)).fetchone()
        qty = (old['quantity'] if old else 0) + delta
        if qty > 99:
            raise ValueError('Максимум 99 порций одного товара')
        if not old and delta > 0 and len(cart(user, c)) >= 30:
            raise ValueError('В корзине может быть максимум 30 разных товаров')
        if qty <= 0:
            c.execute('DELETE FROM cart WHERE user_id=? AND product_id=?', (user, pid))
        else:
            c.execute('INSERT INTO cart VALUES (?,?,?) ON CONFLICT(user_id,product_id) DO UPDATE SET quantity=excluded.quantity', (user, pid, qty))


def clear_cart(user):
    with connect(True) as c:
        c.execute('DELETE FROM cart WHERE user_id=?', (user,))
        c.execute('DELETE FROM drafts WHERE user_id=?', (user,))


def draft(user):
    with connect() as c:
        row = c.execute('SELECT * FROM drafts WHERE user_id=?', (user,)).fetchone()
        return (row['step'], json.loads(row['data'])) if row else (None, {})


def save_draft(user, step, data):
    with connect(True) as c:
        if step is None:
            c.execute('DELETE FROM drafts WHERE user_id=?', (user,))
        else:
            c.execute('INSERT INTO drafts VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET step=excluded.step,data=excluded.data', (user, step, json.dumps(data, ensure_ascii=False)))


def quote(user, data, c=None):
    if c is None:
        with connect() as conn:
            return quote(user, data, conn)
    s = settings(c)
    items = [dict(p) for p in cart(user, c)]
    if s['orders_open'] != '1':
        raise ValueError('Приём заказов сейчас закрыт. Попробуйте в рабочее время.')
    if not items:
        raise ValueError('Корзина пуста')
    if any(not p['active'] for p in items):
        raise ValueError('В корзине есть недоступные товары. Удалите их перед заказом.')
    subtotal = sum(p['price'] * p['quantity'] for p in items)
    if subtotal < int(s['minimum_order']):
        raise ValueError('Минимальная сумма товаров: ' + money(s['minimum_order'], s['currency']))
    method = data.get('method')
    if method not in ('delivery', 'pickup'):
        raise ValueError('Выберите способ получения')
    if method == 'delivery' and s['delivery_enabled'] != '1':
        raise ValueError('Доставка сейчас недоступна. Выберите самовывоз.')
    district = ''
    if method == 'delivery' and s.get('delivery_districts') == '1':
        from .shop_policy import DISTRICTS
        district = data.get('district', '')
        if district not in DISTRICTS:
            raise ValueError('Выберите район доставки. Для другого района или уточнения адреса позвоните ' + s['phone'] + ': стоимость нужно согласовать до заказа и оплаты.')
    free = int(s['free_delivery_from'])
    delivery = int(s['delivery_fee']) if method == 'delivery' and not (free > 0 and subtotal >= free) else 0
    percent = int(s.get('pickup_discount','0')) if method == 'pickup' else 0
    if not 0 <= percent <= 50:
        raise ValueError('Проверьте настройку скидки магазина')
    for p in items:
        p['price'] = max(1, (p['price'] * (100-percent) + 50)//100)
    discount = subtotal - sum(p['price'] * p['quantity'] for p in items)
    from .shop_policy import snapshot
    legal_text, legal_hash = snapshot(s)
    digest = hashlib.sha256(json.dumps([items, s, method, district, legal_hash], sort_keys=True).encode()).hexdigest()
    return {'items': items, 'subtotal': subtotal, 'discount': discount, 'district': district,
            'legal_snapshot': legal_text, 'delivery': delivery, 'total': subtotal - discount + delivery,
            'currency': s['currency'], 'fingerprint': digest}


def order_summary(order, items):
    currency = order['currency']
    lines = [f'Заказ №{order["id"]} · {STATUSES[order["status"]]}',
             *[f'{p["name"]} × {p["quantity"]} — {money(p["price"] * p["quantity"], currency)}' for p in items],
             f'Скидка на самовывоз: {money(dict(order).get("discount",0), currency)}',
             f'Доставка: {money(order["delivery"], currency)}', f'Итого: {money(order["total"], currency)}',
             f'Имя: {order["customer"]}', f'Телефон: {order["phone"]}',
             'Получение: ' + ('Доставка' if order['method'] == 'delivery' else 'Самовывоз'),
             f'Адрес: {order["address"]}', f'Комментарий: {order["comment"] or "—"}',
             'Оплата: ' + ({'cash':'наличными при получении','card':'картой при получении'}.get(dict(order).get('payment_method','cash'), dict(order).get('payment_status','pending')))]
    return '\n'.join(lines)


def enqueue(c, chat_id, text):
    # Telegram messages have a 4096 character limit; splitting preserves all order lines.
    for start in range(0, len(text), 3500):
        c.execute('INSERT INTO outbox(chat_id,text) VALUES (?,?)', (chat_id, text[start:start+3500]))


def place_order(user, token):
    with connect(True) as c:
        old = c.execute('SELECT id FROM orders WHERE token=? AND user_id=?', (token, user)).fetchone()
        if old:
            return old['id']
        row = c.execute('SELECT * FROM drafts WHERE user_id=?', (user,)).fetchone()
        if not row or row['step'] != 'confirm':
            raise ValueError('Оформите заказ заново через корзину')
        d = json.loads(row['data'])
        if d.get('token') != token:
            raise ValueError('Это старое подтверждение. Используйте последнее сообщение.')
        q = quote(user, d, c)
        if q['fingerprint'] != d.get('fingerprint'):
            raise ValueError('Корзина или условия заказа изменились. Откройте корзину и оформите заново.')
        if not all(d.get(k) for k in ('customer', 'phone', 'address')):
            raise ValueError('Не заполнены контактные данные')
        cursor = c.execute('''INSERT INTO orders(token,user_id,customer,phone,method,address,comment,subtotal,delivery,total,currency)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)''', (token, user, d['customer'], d['phone'], d['method'], d['address'], d.get('comment',''), q['subtotal'], q['delivery'], q['total'], q['currency']))
        oid = cursor.lastrowid
        c.execute('UPDATE orders SET discount=?,district=?,legal_snapshot=?,consent_at=strftime(\'%Y-%m-%d %H:%M:%S\',\'now\') WHERE id=?',
                  (q['discount'],q['district'],q['legal_snapshot'],oid))
        c.executemany('INSERT INTO order_items(order_id,name,price,quantity,product_id,iiko_id,iiko_size) VALUES (?,?,?,?,?,?,?)',
                      [(oid, p['name'], p['price'], p['quantity'], p['id'], p['iiko_id'], p['iiko_size']) for p in q['items']])
        order = c.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
        summary = order_summary(order, q['items'])
        enqueue(c, user, 'Спасибо! Заказ получен, ожидайте подтверждения магазина.\n\n' + summary)
        c.execute('UPDATE orders SET notified=0 WHERE id=?',(oid,))
        from .store import notify_order
        notify_order(c,oid)
        from .customer_accounts import new_order
        new_order(c,oid)
        auto_accept(c,oid)
        c.execute('DELETE FROM cart WHERE user_id=?', (user,))
        c.execute('DELETE FROM drafts WHERE user_id=?', (user,))
        return oid


def set_status(oid, status, c=None):
    if c is None:
        with connect(True) as conn:
            return set_status(oid, status, conn)
    order = c.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
    if not order:
        raise ValueError('Заказ не найден')
    if status not in TRANSITIONS[order['status']]:
        raise ValueError('Недопустимый переход статуса. Обновите страницу.')
    if status != 'cancelled' and order['payment_method'] == 'tbank' and order['payment_status'] != 'paid':
        raise ValueError('Онлайн-оплата ещё не подтверждена банком')
    c.execute('UPDATE orders SET status=? WHERE id=?', (status, oid))
    if order['channel'] == 'telegram':
        enqueue(c, order['user_id'], f'Заказ №{oid}: {STATUSES[status]}.')
    else:
        from .order_updates import queue_status
        queue_status(c, dict(order) | {'status':status})
    from .customer_accounts import queue
    queue(c, dict(order) | {'status':status})
    from .max_chat import queue as queue_max
    queue_max(c, dict(order) | {'status':status})
    if status == 'accepted':
        from .integrations import queue_iiko
        queue_iiko(c, oid)


def auto_accept(c, oid):
    order = c.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
    if (settings(c).get('auto_accept') == '1' and order and order['status'] == 'new'
            and (order['payment_method'] != 'tbank' or order['payment_status'] == 'paid')):
        set_status(oid, 'accepted', c)


def login_allowed(ip):
    with connect() as c:
        row = c.execute('SELECT * FROM login_limits WHERE ip=?', (ip,)).fetchone()
        return not row or row['until'] < time.time() or row['attempts'] < 5


def login_result(ip, success):
    with connect(True) as c:
        c.execute('DELETE FROM login_limits WHERE until<?', (time.time(),))
        if success:
            c.execute('DELETE FROM login_limits WHERE ip=?', (ip,))
        else:
            c.execute('INSERT INTO login_limits VALUES (?,1,?) ON CONFLICT(ip) DO UPDATE SET attempts=attempts+1', (ip, time.time() + 900))


def seed():
    init()
    with connect(True) as c:
        if c.execute('SELECT count(*) FROM products').fetchone()[0]:
            return
        for name in ['Роллы', 'Запечённые роллы', 'Сеты', 'Суши', 'Напитки']:
            c.execute('INSERT OR IGNORE INTO categories(name) VALUES (?)', (name,))
        cats = dict((r['name'], r['id']) for r in c.execute('SELECT * FROM categories'))
        for name, cat, ingredients, weight, price in [
            ('Филадельфия', 'Роллы', 'Рис, лосось, сливочный сыр, огурец, нори', '280 г · 8 шт.', 59000),
            ('Калифорния', 'Роллы', 'Рис, снежный краб, огурец, майонез, икра масаго, нори', '250 г · 8 шт.', 49000),
            ('Запечённый лосось', 'Запечённые роллы', 'Рис, лосось, сливочный сыр, сырный соус, нори', '300 г · 8 шт.', 65000),
            ('Сет на двоих', 'Сеты', 'Филадельфия, Калифорния, маки с огурцом', '720 г · 24 шт.', 139000),
            ('Суши с лососем', 'Суши', 'Рис, лосось', '35 г · 1 шт.', 16000)]:
            c.execute('INSERT INTO products(category_id,name,ingredients,weight,price) VALUES (?,?,?,?,?)', (cats[cat], name, ingredients, weight, price))


def admin_ids():
    from .vault import get_config
    cfg = get_config('telegram')
    return cfg.get('admin_ids', config.ADMIN_IDS) if cfg.get('enabled', True) else []
