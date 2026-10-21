import json

from app import db, menu_import
from app.vault import set_config
from test_admin import login
from test_menu_import import CFG, PRODUCT, batch, menu


def test_order_requires_login_csrf_and_complete_category_membership(client):
    assert client.get('/admin/menu-order', follow_redirects=False).status_code == 303
    token = login(client)
    ids = [c['id'] for c in db.categories()][::-1]
    payload = {'csrf': token, 'kind': 'categories', 'category': ids[0], 'ids': json.dumps(ids)}
    assert client.post('/admin/menu-order', data={**payload, 'csrf': 'wrong'}).status_code == 403
    response = client.post('/admin/menu-order', data=payload)
    assert 'Порядок меню сохранён' in response.text
    assert [c['id'] for c in db.categories()] == ids
    for invalid in (ids[:-1], ids+[ids[0]], [True], {'id': 1}, ['1'], [999999]):
        response = client.post('/admin/menu-order', data={**payload, 'ids': json.dumps(invalid)})
        assert 'Не удалось сохранить порядок' in response.text
        assert [c['id'] for c in db.categories()] == ids


def test_large_menu_order_uses_one_field_and_new_items_go_last(client):
    token = login(client)
    category = db.categories()[0]['id']
    with db.connect(True) as c:
        c.executemany("INSERT INTO products(category_id,name,price,ingredients) VALUES (?,?,?,'')",
                      [(category, f'Блюдо {i}', 100) for i in range(40)])
    ids = [p['id'] for p in db.products(category=category)][::-1]
    response = client.post('/admin/menu-order', data={'csrf': token, 'kind': 'products',
                            'category': category, 'ids': json.dumps(ids)})
    assert response.status_code == 200 and 'Порядок меню сохранён' in response.text
    assert [p['id'] for p in db.products(category=category)] == ids
    with db.connect(True) as c:
        new_id = c.execute("INSERT INTO products(category_id,name,price,ingredients) VALUES (?,?,?,'')",
                           (category, 'Новое блюдо', 100)).lastrowid
    assert [p['id'] for p in db.products(category=category)] == ids+[new_id]
    response = client.post('/admin/menu-order', data={'csrf': token, 'kind': 'products',
                            'category': category, 'ids': json.dumps(ids)})
    assert 'Не удалось сохранить порядок' in response.text


def test_iiko_updates_preserve_manual_positions(shop):
    set_config('iiko', CFG)
    menu_import.apply_import(batch(menu()))
    with db.connect(True) as c:
        row = c.execute('SELECT id,category_id FROM products WHERE iiko_id=?', (PRODUCT,)).fetchone()
        c.execute('UPDATE products SET position=0 WHERE id=?', (row['id'],))
        c.execute('UPDATE categories SET position=0 WHERE id=?', (row['category_id'],))
    menu_import.apply_import(batch(menu()))
    assert db.categories()[0]['id'] == row['category_id']
    assert db.products()[0]['id'] == row['id']
