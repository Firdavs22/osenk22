import json
import uuid

import pytest

from app import db, integrations
from test_store import draft, session


PARTS = {'city': 'Казань', 'street': 'Юлиуса Фучика', 'house': '14В',
         'building': '1', 'flat': '219', 'entrance': '2', 'floor': '5', 'doorphone': '219#'}


def delivery(client):
    headers = session(client)
    data = draft(client, headers)
    with db.connect(True) as c:
        c.execute("UPDATE settings SET value='0' WHERE key='delivery_fee'")
    quote = client.post('/api/quote', headers=headers, json={'method': 'delivery'}).json()
    return headers, {**data, **quote, 'method': 'delivery', 'address_parts': PARTS.copy()}


def test_structured_checkout_snapshot_idempotency_and_iiko(client):
    product_id = str(uuid.uuid4())
    with db.connect(True) as c:
        c.execute('UPDATE products SET iiko_id=? WHERE id=1', (product_id,))
    headers, data = delivery(client)
    data['address_parts']['city'] = '  Казань  '
    response = client.post('/api/checkout', headers=headers, json=data)
    assert response.status_code == 200, response.text
    data['address_parts']['flat'] = '999'
    assert client.post('/api/checkout', headers=headers, json=data).json() == response.json()
    with db.connect() as c:
        order = dict(c.execute('SELECT * FROM orders').fetchone())
        assert json.loads(order['address_parts']) == PARTS
        assert 'Юлиуса Фучика, д. 14В' in order['address']
        assert 'домофон 219#' in order['address'] and '219' in order['address']
        message = c.execute('SELECT text FROM outbox WHERE chat_id=999').fetchone()[0]
        assert order['address'] in message
    assert order['address'] in client.get(response.json()['url']).text
    cfg = {'city_format': True, 'organization_id': str(uuid.uuid4()), 'terminal_group': str(uuid.uuid4())}
    payload = integrations.iiko_payload({'order_id': order['id'], 'external_id': str(uuid.uuid4())}, cfg)
    address = payload['order']['deliveryPoint']['address']
    assert address == {'type': 'city', 'line1': 'Казань, Юлиуса Фучика, д. 14В, корп. / стр. 1',
                       'flat': '219', 'entrance': '2', 'floor': '5', 'doorphone': '219#'}


@pytest.mark.parametrize('parts', [None, [], {}, {**PARTS, 'house': ''},
    {**PARTS, 'city': '   '}, {**PARTS, 'street': 5}, {**PARTS, 'doorphone': '1'*11}])
def test_invalid_address_rejected_before_order_creation(client, parts):
    headers, data = delivery(client)
    data['address_parts'] = parts
    data['address'] = 'Нельзя обойти проверку новым адресом'
    assert client.post('/api/checkout', headers=headers, json=data).status_code == 400
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM orders').fetchone()[0] == 0
    assert client.get('/api/cart').json()['quantity'] == 1


def test_house_without_apartment_and_legacy_checkout(client):
    headers, data = delivery(client)
    data['address_parts'] = {k: PARTS[k] for k in ('city', 'street', 'house')}
    response = client.post('/api/checkout', headers=headers, json=data)
    assert response.status_code == 200, response.text
    with db.connect() as c:
        assert c.execute('SELECT address FROM orders').fetchone()[0] == 'Казань, Юлиуса Фучика, д. 14В'
    headers, data = delivery(client)
    data.pop('address_parts')
    data['address'] = 'Казань, Петербургская 64, кв. 1'
    assert client.post('/api/checkout', headers=headers, json=data).status_code == 200
    with db.connect() as c:
        old = c.execute('SELECT address,address_parts FROM orders ORDER BY id DESC').fetchone()
        assert old['address'] == data['address'] and json.loads(old['address_parts']) == {}


def test_pickup_discards_delivery_fields(client):
    headers = session(client)
    data = draft(client, headers)
    data['address_parts'] = PARTS
    assert client.post('/api/checkout', headers=headers, json=data).status_code == 200
    with db.connect() as c:
        order = c.execute('SELECT address,address_parts FROM orders').fetchone()
        assert order['address'] == db.settings()['address']
        assert json.loads(order['address_parts']) == {}
