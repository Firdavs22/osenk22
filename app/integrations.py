"""Durable integration jobs. No external API is called inside an order transaction."""
import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import PlainTextResponse

from . import db
from .vault import get_config, seal, unseal

log = logging.getLogger(__name__)
BANK_API = 'https://securepay.tinkoff.ru/v2/'
IIKO_API = 'https://api-ru.iiko.services'
TAXES = ('none', 'vat0', 'vat5', 'vat7', 'vat10', 'vat22', 'vat105', 'vat107', 'vat110', 'vat122')
TAXATIONS = ('osn', 'usn_income', 'usn_income_outcome', 'esn', 'patent')


def uuid_field(value):
    if not value:
        return ''
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise ValueError('Идентификатор iiko должен быть UUID')


def payment_enabled():
    cfg = get_config('tbank')
    return bool(cfg.get('enabled') and cfg.get('terminal') and cfg.get('password') and
                cfg.get('public_url') and cfg.get('fiscal_ready') and db.settings()['currency'] in ('₽', 'RUB', 'руб.'))


def bank_token(data, password):
    scalar = {k: v for k, v in data.items() if k != 'Token' and not isinstance(v, (dict, list)) and v is not None}
    scalar['Password'] = password
    def stringify(v):
        return str(v).lower() if isinstance(v, bool) else str(v)
    return hashlib.sha256(''.join(stringify(scalar[k]) for k in sorted(scalar)).encode()).hexdigest()


async def bank_call(cfg, method, data):
    body = {'TerminalKey': cfg['terminal'], **data}
    body['Token'] = bank_token(body, cfg['password'])
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        response = await client.post(BANK_API + method, json=body)
        response.raise_for_status()
        result = response.json()
    if result.get('Success') is not True:
        # Never log/echo provider responses: they can contain credentials and personal data.
        raise ValueError('Банк отклонил запрос. Проверьте операцию в кабинете эквайринга.')
    return result


def create_payment(c, oid):
    cfg = get_config('tbank', c)
    order = c.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
    items = c.execute('SELECT * FROM order_items WHERE order_id=?', (oid,)).fetchall()
    receipt_items = [{'Name': p['name'][:128], 'Price': p['price'], 'Quantity': p['quantity'],
                      'Amount': p['price']*p['quantity'], 'Tax': cfg['tax'], 'PaymentMethod': 'full_prepayment',
                      'PaymentObject': 'commodity', 'MeasurementUnit': 'шт'} for p in items]
    if order['delivery']:
        receipt_items.append({'Name': 'Доставка', 'Price': order['delivery'], 'Quantity': 1, 'Amount': order['delivery'],
                              'Tax': cfg['delivery_tax'], 'PaymentMethod': 'full_prepayment', 'PaymentObject': 'service', 'MeasurementUnit': 'шт'})
    receipt = {'FfdVersion': '1.2', 'Phone': order['phone'], 'Taxation': cfg['taxation'], 'Items': receipt_items}
    c.execute('INSERT INTO payments(order_id,bank_order,credentials,receipt,expires) VALUES (?,?,?,?,?)',
              (oid, 'web-' + uuid.uuid4().hex, seal(cfg), json.dumps(receipt, ensure_ascii=False), time.time()+1800))


def payment_row(oid):
    with db.connect() as c:
        row = c.execute('SELECT p.*,o.total,o.public_token,o.payment_status,o.status order_status FROM payments p JOIN orders o ON o.id=p.order_id WHERE order_id=?', (oid,)).fetchone()
    return dict(row) if row else None


def apply_payment(row, result):
    cfg = unseal(row['credentials'])
    if (str(result.get('TerminalKey')) != cfg['terminal'] or str(result.get('PaymentId')) != row['payment_id'] or
            type(result.get('Amount')) is not int or result['Amount'] != row['total']):
        raise ValueError('Данные платежа не совпали с заказом. Нужна проверка администратора.')
    if result.get('OrderId') is not None and str(result['OrderId']) != row['bank_order']:
        raise ValueError('Номер заказа в банке не совпал')
    status = result.get('Status', '')
    mapped = {'CONFIRMED': 'paid', 'REFUNDED': 'refunded', 'PARTIAL_REFUNDED': 'partial_refund',
              'REJECTED': 'failed', 'CANCELED': 'failed', 'DEADLINE_EXPIRED': 'failed', 'REVERSED': 'failed'}.get(status)
    with db.connect(True) as c:
        current = c.execute('SELECT payment_status FROM orders WHERE id=?', (row['order_id'],)).fetchone()[0]
        # A late AUTHORIZED/CONFIRMED notification cannot undo a refund or a settled payment.
        if mapped and not (current in ('refunded', 'partial_refund') and mapped == 'paid') and not (current == 'paid' and mapped == 'failed'):
            c.execute('UPDATE orders SET payment_status=? WHERE id=?', (mapped, row['order_id']))
            if mapped == 'paid':
                from .store import notify_order
                notify_order(c, row['order_id'])
                if current != 'paid':
                    db.auto_accept(c, row['order_id'])
        c.execute('UPDATE payments SET state=?,error=?,next_try=? WHERE order_id=?',
                  (status or 'pending', '', time.time()+60, row['order_id']))


async def process_payment(oid):
    row = payment_row(oid)
    cfg = unseal(row['credentials'])
    if not row['payment_id']:
        if row['state'] == 'new':
            # Commit BEFORE calling Init. An ambiguous response is recovered via CheckOrder,
            # never by automatically creating another charge.
            with db.connect(True) as c:
                claimed = c.execute("UPDATE payments SET state='initializing',attempts=attempts+1,next_try=? WHERE order_id=? AND state='new'", (time.time()+60, oid)).rowcount
            if not claimed:
                return
            origin = cfg['public_url'].rstrip('/')
            result = await bank_call(cfg, 'Init', {'Amount': row['total'], 'OrderId': row['bank_order'],
                'Description': f'Заказ №{oid}', 'PayType': 'O', 'Receipt': json.loads(row['receipt']),
                'NotificationURL': origin+'/api/payments/tbank/webhook',
                'SuccessURL': origin+'/order/'+row['public_token'], 'FailURL': origin+'/order/'+row['public_token'],
                'RedirectDueDate': datetime.fromtimestamp(row['expires'], timezone.utc).isoformat(timespec='seconds')})
            url = result.get('PaymentURL', '')
            parsed = urlparse(url)
            if parsed.scheme != 'https' or parsed.hostname not in ('securepay.tinkoff.ru', 'securepay.tbank.ru'):
                raise ValueError('Банк вернул неизвестный адрес оплаты; нужна проверка')
            if result.get('Amount') != row['total'] or str(result.get('OrderId')) != row['bank_order'] or not result.get('PaymentId'):
                raise ValueError('Банк вернул несовпадающие параметры заказа')
            with db.connect(True) as c:
                c.execute('UPDATE payments SET payment_id=?,url=?,state=?,next_try=?,error=? WHERE order_id=?',
                          (str(result['PaymentId']),url,'NEW',time.time()+15,'',oid))
            return
        result = await bank_call(cfg, 'CheckOrder', {'OrderId': row['bank_order']})
        payments = result.get('Payments', [])
        if len(payments) != 1:
            raise ValueError('Создание платежа не подтверждено. Проверьте заказ в кабинете банка; повторного списания не будет.')
        with db.connect(True) as c:
            c.execute('UPDATE payments SET payment_id=? WHERE order_id=?', (str(payments[0]['PaymentId']), oid))
        row = payment_row(oid)
    result = await bank_call(cfg, 'GetState', {'PaymentId': row['payment_id']})
    apply_payment(row, result)


async def bank_webhook(request: Request):
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError()
        with db.connect() as c:
            found = c.execute('SELECT order_id FROM payments WHERE bank_order=?', (str(data.get('OrderId', '')),)).fetchone()
        if not found:
            raise ValueError()
        row = payment_row(found['order_id'])
        cfg = unseal(row['credentials'])
        if not hmac.compare_digest(str(data.get('Token', '')).encode(), bank_token(data, cfg['password']).encode()):
            raise ValueError()
        if str(data.get('TerminalKey')) != cfg['terminal']:
            raise ValueError()
        # A signed callback wakes reconciliation. Only GetState changes financial state.
        with db.connect(True) as c:
            c.execute('UPDATE payments SET next_try=0,attempts=0 WHERE order_id=?', (row['order_id'],))
        return PlainTextResponse('OK')
    except (ValueError, TypeError):
        raise HTTPException(400, 'Invalid notification')


async def iiko_call(cfg, path, body, token=None):
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        if token is None:
            response = await client.post(IIKO_API+'/api/v2/access_token', json={
                'apiKey': cfg['api_key'], 'appId': cfg['app_id'], 'clientSecret': cfg['client_secret']})
            response.raise_for_status()
            token = response.json()['token']
        response = await client.post(IIKO_API+path, json=body, headers={'Authorization': 'Bearer '+token})
        response.raise_for_status()
        return response.json()


def queue_iiko(c, oid):
    cfg = get_config('iiko', c)
    if cfg.get('enabled'):
        c.execute('INSERT OR IGNORE INTO iiko_jobs(order_id,external_id,credentials) VALUES (?,?,?)', (oid,str(uuid.uuid4()),seal(cfg)))


def iiko_payload(row, cfg):
    with db.connect() as c:
        order = dict(c.execute('SELECT * FROM orders WHERE id=?', (row['order_id'],)).fetchone())
        products = c.execute('SELECT * FROM order_items WHERE order_id=?', (row['order_id'],)).fetchall()
    items = []
    for p in products:
        if not p['iiko_id']:
            raise ValueError('В заказе есть товар без UUID iiko. Сопоставьте товары до оформления следующего заказа.')
        item = {'type': 'Product', 'productId': p['iiko_id'], 'amount': p['quantity'], 'price': p['price']/100}
        if p['iiko_size']:
            item['productSizeId'] = p['iiko_size']
        items.append(item)
    if order['delivery']:
        if not cfg.get('delivery_product'):
            raise ValueError('Укажите UUID услуги доставки в интеграции iiko')
        items.append({'type': 'Product', 'productId': cfg['delivery_product'], 'amount': 1, 'price': order['delivery']/100})
    body = {'id': row['external_id'], 'externalNumber': str(order['id']), 'phone': order['phone'],
            'orderServiceType': 'DeliveryByCourier' if order['method']=='delivery' else 'DeliveryByClient',
            'items': items, 'comment': f"{order['customer']}. {order['comment']}",
            'customer': {'type': 'one-time', 'name': order['customer']}}
    if order['method']=='delivery':
        # Free-form address is supported by AddressCity on restaurants configured for City.
        if not cfg.get('city_format'):
            raise ValueError('Для доставки включите формат адреса City в iiko и настройках интеграции')
        if len(order['address']) > 250:
            raise ValueError('Для iiko адрес должен быть не длиннее 250 символов')
        body['deliveryPoint'] = {'address': {'type': 'city', 'line1': order['address'][:250]}}
    if order['payment_status']=='paid':
        if not cfg.get('payment_type'):
            raise ValueError('Укажите UUID типа внешней оплаты iiko')
        body['payments'] = [{'paymentTypeKind': 'Card', 'paymentTypeId': cfg['payment_type'], 'sum': order['total']/100, 'isProcessedExternally': True}]
    return {'organizationId': cfg['organization_id'], 'terminalGroupId': cfg['terminal_group'],
            'createOrderSettings': {'checkStopList': True}, 'order': body}


async def process_iiko(oid):
    with db.connect() as c:
        row = dict(c.execute('SELECT * FROM iiko_jobs WHERE order_id=?', (oid,)).fetchone())
        status = c.execute('SELECT status FROM orders WHERE id=?', (oid,)).fetchone()[0]
    cfg = unseal(row['credentials'])
    if row['state'] == 'pending':
        if status == 'cancelled':
            with db.connect(True) as c:
                c.execute("UPDATE iiko_jobs SET state='cancelled' WHERE order_id=?", (oid,))
            return
        body = iiko_payload(row, cfg)
        with db.connect(True) as c:
            claimed = c.execute("UPDATE iiko_jobs SET state='checking',payload=?,next_try=?,attempts=attempts+1 WHERE order_id=? AND state='pending'",
                                (json.dumps(body,ensure_ascii=False),time.time()+60,oid)).rowcount
        if not claimed:
            return
        # Once attempted, subsequent runs ONLY query this UUID, including after a timeout.
        await iiko_call(cfg, '/api/1/deliveries/create', body)
    result = await iiko_call(cfg, '/api/1/deliveries/by_id', {'organizationId': cfg['organization_id'], 'orderIds': [row['external_id']]})
    found = next((o for o in result.get('orders', []) if o.get('id') == row['external_id']), None)
    state = found.get('creationStatus') if found else None
    with db.connect(True) as c:
        c.execute('UPDATE iiko_jobs SET state=?,next_try=?,error=? WHERE order_id=?',
                  ('sent' if state=='Success' else 'failed' if state=='Error' else 'checking', time.time()+60,
                   'iiko отклонила заказ. Проверьте UUID, стоп-лист и журнал iiko.' if state=='Error' else '', oid))


async def tick():
    now = time.time()
    with db.connect() as c:
        payments = c.execute("SELECT order_id FROM payments WHERE next_try<=? AND attempts<120 AND state NOT IN ('REFUNDED','REJECTED','CANCELED','DEADLINE_EXPIRED','REVERSED') ORDER BY next_try LIMIT 5", (now,)).fetchall()
        jobs = c.execute("SELECT order_id FROM iiko_jobs WHERE next_try<=? AND state IN ('pending','checking') AND attempts<60 LIMIT 5", (now,)).fetchall()
    for table, rows, fn in [('payments', payments, process_payment), ('iiko_jobs', jobs, process_iiko)]:
        for row in rows:
            oid = row['order_id']
            with db.connect(True) as c:
                c.execute(f'UPDATE {table} SET next_try=?,attempts=attempts+1 WHERE order_id=?', (now+60,oid))
            try:
                await fn(oid)
                if table == 'payments':
                    with db.connect(True) as c:
                        # Keep checking settled payments for later refunds, without a hot loop.
                        c.execute("UPDATE payments SET next_try=?,attempts=0 WHERE order_id=? AND state IN ('CONFIRMED','PARTIAL_REFUNDED')", (time.time()+3600,oid))
            except Exception as exc:
                message = str(exc) if type(exc) is ValueError else 'Нет подтверждения от сервиса. Повторим проверку автоматически.'
                with db.connect(True) as c:
                    c.execute(f'UPDATE {table} SET error=? WHERE order_id=?', (message, oid))
                log.warning('%s order %s: %s', table, oid, type(exc).__name__)


async def worker():
    while True:
        try:
            await tick()
            from .iiko_status import tick as sync_statuses
            await sync_statuses()
        except Exception as exc:
            log.warning('Integration worker: %s', type(exc).__name__)
        await asyncio.sleep(3)
