"""Customer accounts belong to a verified messenger contact, never a typed phone."""
import hashlib
import hmac
import re
import time

from . import db

CONTACT_PROMPT = ('Личный кабинет: заказы с сайта и уведомления о статусах. '
    'Нажмите «Поделиться своим номером» и подтвердите отправку контакта. '
    'Номер в мессенджере должен совпадать с телефоном заказа. '
    'Введённый вручную или чужой контакт не открывает историю. '
    'Уведомления можно отключить: /stopupdates. Выход: /logout.')


def normalize_phone(value):
    value = re.sub(r'[\s()\-]', '', str(value or ''))
    if not re.fullmatch(r'\+?[0-9]{10,15}', value):
        return ''
    digits = value.lstrip('+')
    if len(digits) == 10:
        digits = '7' + digits
    if len(digits) == 11 and digits.startswith('8'):
        digits = '7' + digits[1:]
    return '+' + digits


def max_contact_phone(event, token):
    """MAX request_contact HMAC, sender ownership and no forwarded contacts."""
    message = event.get('message') or {}
    sender = message.get('sender') or {}
    if message.get('link') or sender.get('is_bot'):
        return ''
    contacts = [a.get('payload') or {} for a in (message.get('body') or {}).get('attachments') or []
                if a.get('type') == 'contact']
    if len(contacts) != 1:
        return ''
    contact = contacts[0]
    uid = sender.get('user_id')
    if type(uid) is not int or (contact.get('max_info') or {}).get('user_id') != uid:
        return ''
    vcard, signature = contact.get('vcf_info'), contact.get('hash')
    if not isinstance(vcard, str) or len(vcard) > 12000 or not isinstance(signature, str) or not re.fullmatch(r'[0-9a-fA-F]{64}',signature):
        return ''
    # JSON decoding normally already produces CRLF; also handle documented escaped CRLF.
    vcard = vcard.replace('\\r\\n', '\r\n')
    expected = hmac.new(token.encode(), vcard.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.lower()):
        return ''
    phones = re.findall(r'^TEL(?:;[^:\r\n]*)?:([^\r\n]+)', vcard, re.MULTILINE | re.IGNORECASE)
    return normalize_phone(phones[0]) if len(phones) == 1 else ''


def begin_contact(platform, bot_key, user_id, chat_id):
    with db.connect(True) as c:
        c.execute('DELETE FROM customer_contact_requests WHERE expires<?', (time.time(),))
        c.execute('''INSERT INTO customer_contact_requests VALUES (?,?,?,?,?)
            ON CONFLICT(platform,bot_key,user_id) DO UPDATE SET chat_id=excluded.chat_id,expires=excluded.expires''',
                  (platform, str(bot_key), user_id, chat_id, time.time()+600))


def waiting_contact(platform, bot_key, user_id, chat_id):
    with db.connect() as c:
        return bool(c.execute('''SELECT 1 FROM customer_contact_requests
            WHERE platform=? AND bot_key=? AND user_id=? AND chat_id=? AND expires>?''',
            (platform, str(bot_key), user_id, chat_id, time.time())).fetchone())


def cancel_contact(platform, bot_key, user_id):
    with db.connect(True) as c:
        c.execute('DELETE FROM customer_contact_requests WHERE platform=? AND bot_key=? AND user_id=?',
                  (platform,str(bot_key),user_id))


def verify_contact(platform, bot_key, user_id, chat_id, phone):
    """Called only after the adapter authenticates the contact. Consume explicit intent."""
    phone = normalize_phone(phone)
    if not phone:
        raise ValueError('Не удалось подтвердить номер. Отправьте свой контакт кнопкой.')
    key = (platform, str(bot_key), user_id)
    with db.connect(True) as c:
        intent = c.execute('SELECT * FROM customer_contact_requests WHERE platform=? AND bot_key=? AND user_id=?', key).fetchone()
        if not intent or intent['chat_id'] != chat_id or intent['expires'] < time.time():
            raise ValueError('Запрос истёк. Откройте /account и снова поделитесь своим номером.')
        old = c.execute('SELECT * FROM customer_accounts WHERE platform=? AND bot_key=? AND user_id=?', key).fetchone()
        if old and old['phone'] != phone:
            raise ValueError('Подключён другой номер. Сначала выйдите через /logout, затем подтвердите новый номер.')
        other = c.execute('SELECT user_id FROM customer_accounts WHERE platform=? AND bot_key=? AND phone=?', (*key[:2], phone)).fetchone()
        if other and other['user_id'] != user_id:
            raise ValueError('Этот номер уже связан с другим аккаунтом. Для смены привязки обратитесь в магазин.')
        c.execute('''INSERT INTO customer_accounts(platform,bot_key,user_id,chat_id,phone,verified_at)
            VALUES (?,?,?,?,?,?) ON CONFLICT(platform,bot_key,user_id) DO UPDATE SET
            chat_id=excluded.chat_id,verified_at=excluded.verified_at,notifications=1''', (*key, chat_id, phone, time.time()))
        account = c.execute('SELECT * FROM customer_accounts WHERE platform=? AND bot_key=? AND user_id=?', key).fetchone()
        c.execute('INSERT OR IGNORE INTO customer_orders SELECT ?,id FROM orders WHERE phone_key=?', (account['id'], phone))
        c.execute('DELETE FROM customer_contact_requests WHERE platform=? AND bot_key=? AND user_id=?', key)
        return account['id']


def stop(platform, bot_key, user_id, logout=False):
    with db.connect(True) as c:
        args = (platform, str(bot_key), user_id)
        c.execute('DELETE FROM customer_contact_requests WHERE platform=? AND bot_key=? AND user_id=?', args)
        c.execute('''DELETE FROM customer_messages WHERE sent<>1 AND account_id IN
            (SELECT id FROM customer_accounts WHERE platform=? AND bot_key=? AND user_id=?)''', args)
        if logout:
            # Cascades only account links/queue, never the orders or messenger's history.
            c.execute('DELETE FROM customer_accounts WHERE platform=? AND bot_key=? AND user_id=?', args)
        else:
            c.execute('UPDATE customer_accounts SET notifications=0 WHERE platform=? AND bot_key=? AND user_id=?', args)


def get_account(platform, bot_key, user_id, c):
    return c.execute('SELECT * FROM customer_accounts WHERE platform=? AND bot_key=? AND user_id=?',
                     (platform, str(bot_key), user_id)).fetchone()


def history(platform, bot_key, user_id, before=0, compact=False):
    """Keyset pagination, re-authorized on every request; no phone in callback payloads."""
    with db.connect() as c:
        account = get_account(platform, bot_key, user_id, c)
        aid = account['id'] if account else -1
        native = user_id if platform == 'telegram' else -1
        orders = c.execute('''SELECT o.* FROM orders o WHERE
            (EXISTS(SELECT 1 FROM customer_orders co WHERE co.order_id=o.id AND co.account_id=?)
            OR (o.channel='telegram' AND o.user_id=? AND ?>0))
            AND (?=0 OR o.id<?) ORDER BY o.id DESC LIMIT 6''', (aid,native,native,before,before)).fetchall()
        texts = []
        for o in orders[:5]:
            items = c.execute('SELECT * FROM order_items WHERE order_id=?',(o['id'],)).fetchall()
            if compact:
                details = ', '.join(f'{p["name"]} × {p["quantity"]}' for p in items)
                if len(details)>400:
                    details=details[:397]+'…'
                text = (f'Заказ №{o["id"]} · {db.STATUSES[o["status"]]}\n{details}\n'
                        f'Итого: {db.money(o["total"],o["currency"])} · '
                        + ('Самовывоз' if o['method']=='pickup' else 'Доставка'))
            else:
                text = db.order_summary(o,items)
            texts.append(text+'\nОформлен: '+o['created_at']+' UTC')
        return texts, orders[4]['id'] if len(orders)>5 else 0, bool(account)


def new_order(c, oid):
    order = c.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
    phone = normalize_phone(order['phone'])
    c.execute('UPDATE orders SET phone_key=? WHERE id=?',(phone,oid))
    if phone:
        c.execute('INSERT OR IGNORE INTO customer_orders SELECT id,? FROM customer_accounts WHERE phone=?',(oid,phone))
    queue(c, order, 'created')


def queue(c, order, event='status'):
    accounts = c.execute('''SELECT a.* FROM customer_accounts a JOIN customer_orders co ON co.account_id=a.id
        WHERE co.order_id=? AND a.notifications=1''',(order['id'],)).fetchall()
    for account in accounts:
        # Native Telegram receipts/status already have their own persistent outbox.
        if account['platform']=='telegram' and order['channel']=='telegram' and account['user_id']==order['user_id']:
            continue
        if account['platform']=='telegram' and c.execute('SELECT 1 FROM order_subscriptions WHERE order_id=? AND chat_id=? AND bot_id=?',
                (order['id'],account['chat_id'],int(account['bot_key']))).fetchone() and event=='status':
            continue
        if event=='created':
            text = 'Заказ получен. Ожидайте подтверждения магазина.\n\n' + db.order_summary(order,
                c.execute('SELECT * FROM order_items WHERE order_id=?',(order['id'],)).fetchall())
        else:
            text = f'Заказ №{order["id"]}: {db.STATUSES[order["status"]]}.'
        text += '\nИстория: /orders. Отключить уведомления: /stopupdates.'
        for part, offset in enumerate(range(0,len(text),3500)):
            c.execute('INSERT OR IGNORE INTO customer_messages(account_id,order_id,event,text) VALUES (?,?,?,?)',
                (account['id'],order['id'],f'{event}:{order["status"]}:{part}',text[offset:offset+3500]))


def claim(platform, bot_key):
    now = time.time()
    with db.connect(True) as c:
        c.execute("UPDATE customer_messages SET sent=-1,error='Исчерпаны попытки отправки' WHERE sent=0 AND attempts>=8 AND next_try<=?",(now,))
        row = c.execute('''SELECT m.*,a.chat_id FROM customer_messages m JOIN customer_accounts a ON a.id=m.account_id
            WHERE a.platform=? AND a.bot_key=? AND a.notifications=1 AND m.sent=0 AND m.next_try<=? AND m.attempts<8
            AND NOT EXISTS(SELECT 1 FROM customer_messages older WHERE older.account_id=m.account_id AND older.id<m.id AND older.sent=0)
            ORDER BY m.id LIMIT 1''',(platform,str(bot_key),now)).fetchone()
        if row:
            c.execute('UPDATE customer_messages SET attempts=attempts+1,next_try=? WHERE id=?',(now+60,row['id']))
        return row


def complete(mid, state, error='', delay=30):
    with db.connect(True) as c:
        c.execute('UPDATE customer_messages SET sent=?,error=?,next_try=? WHERE id=?',(state,error,time.time()+delay,mid))


def links():
    from .order_updates import telegram_state
    from .vault import get_config
    result = {}
    tg = telegram_state()
    if tg['enabled'] and tg['username']:
        result['telegram'] = f'https://t.me/{tg["username"]}?start=account'
    cfg = get_config('max')
    name = cfg.get('bot_username','')
    if cfg.get('enabled') and cfg.get('subscribed') and cfg.get('token') and re.fullmatch(r'[A-Za-z0-9_]{1,80}',name):
        result['max'] = f'https://max.ru/{name}?start=account'
    return result
