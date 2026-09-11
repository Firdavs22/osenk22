import secrets
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import config, db


def prepare(user=100, method='delivery'):
    db.cart_change(user, 1, 1)
    data = {'method': method, 'customer': 'Иван', 'phone': '+79991234567',
            'address': 'Москва, улица Тестовая, 12', 'comment': 'Без имбиря'}
    q = db.quote(user, data)
    data.update(token=secrets.token_hex(12), fingerprint=q['fingerprint'])
    db.save_draft(user, 'confirm', data)
    return data


def test_order_atomic_and_idempotent(shop):
    data = prepare()
    with ThreadPoolExecutor(max_workers=3) as pool:
        ids = list(pool.map(lambda _: db.place_order(100, data['token']), range(3)))
    assert ids == [1, 1, 1]
    assert not db.cart(100)
    with db.connect() as c:
        order = c.execute('SELECT * FROM orders').fetchone()
        assert order['total'] == 79000
        assert c.execute('SELECT count(*) FROM outbox').fetchone()[0] == 2


def test_token_does_not_belong_to_another_user(shop):
    d = prepare()
    db.place_order(100, d['token'])
    with pytest.raises(ValueError):
        db.place_order(200, d['token'])


@pytest.mark.parametrize('change', ['price', 'active', 'cart', 'settings', 'closed'])
def test_revalidate_confirmation(shop, change):
    d = prepare()
    with db.connect(True) as c:
        if change == 'price':
            c.execute('UPDATE products SET price=99900 WHERE id=1')
        elif change == 'active':
            c.execute('UPDATE products SET active=0 WHERE id=1')
        elif change == 'cart':
            c.execute('UPDATE cart SET quantity=2 WHERE user_id=100')
        elif change == 'settings':
            c.execute("UPDATE settings SET value='50000' WHERE key='delivery_fee'")
        else:
            c.execute("UPDATE settings SET value='0' WHERE key='orders_open'")
    with pytest.raises(ValueError):
        db.place_order(100, d['token'])
    assert db.cart(100)
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM orders').fetchone()[0] == 0


def test_order_keeps_original_price_after_edit(shop):
    d = prepare(method='pickup')
    oid = db.place_order(100, d['token'])
    with db.connect(True) as c:
        c.execute("UPDATE products SET name='Новое название',price=10000 WHERE id=1")
    with db.connect() as c:
        item = c.execute('SELECT * FROM order_items WHERE order_id=?', (oid,)).fetchone()
        assert item['name'] == 'Филадельфия'
        assert item['price'] == 59000


def test_delivery_minimum_availability(shop):
    db.cart_change(100, 1, 4)
    assert db.quote(100, {'method': 'delivery'})['delivery'] == 0
    assert db.quote(100, {'method': 'pickup'})['delivery'] == 0
    with db.connect(True) as c:
        c.execute("UPDATE settings SET value='0' WHERE key='free_delivery_from'")
    assert db.quote(100, {'method': 'delivery'})['delivery'] == 20000
    with db.connect(True) as c:
        c.execute("UPDATE settings SET value='300000' WHERE key='minimum_order'")
    with pytest.raises(ValueError, match='Минимальная'):
        db.quote(100, {'method': 'pickup'})


def test_cart_quantity_bounds(shop):
    db.cart_change(100, 1, 99)
    with pytest.raises(ValueError):
        db.cart_change(100, 1, 1)
    db.cart_change(100, 1, -99)
    assert not db.cart(100)


def test_status_workflow_and_notifications(shop):
    d = prepare()
    oid = db.place_order(100, d['token'])
    with pytest.raises(ValueError):
        db.set_status(oid, 'done')
    for state in ('accepted', 'cooking', 'ready', 'done'):
        db.set_status(oid, state)
    with pytest.raises(ValueError):
        db.set_status(oid, 'cancelled')
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM outbox').fetchone()[0] == 6


@pytest.mark.parametrize('value', ['-1', 'NaN', 'Infinity', '0.001', '1000001', 'bad'])
def test_money_invalid(value):
    with pytest.raises(ValueError):
        db.parse_money(value)


def test_money_exact():
    assert db.parse_money('123,45') == 12345
    assert db.parse_money('0.29') == 29


def test_backup_consistent(shop, tmp_path):
    import zipfile
    from app.manage import backup
    d = prepare()
    db.place_order(100, d['token'])
    archive = backup(tmp_path / 'backups')
    with zipfile.ZipFile(archive) as z:
        z.extract('shop.sqlite3', tmp_path / 'restore')
        assert '.env' not in z.namelist()
    with sqlite3.connect(tmp_path / 'restore/shop.sqlite3') as c:
        assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert c.execute('SELECT count(*) FROM orders').fetchone()[0] == 1
