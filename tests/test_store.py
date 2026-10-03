import asyncio
import re

from app import db
from app.vault import get_config, set_config
from test_admin import login


def session(client):
    page = client.get('/')
    token = re.search(r'name="csrf-token" content="([^"]+)"',page.text).group(1)
    return {'x-csrf-token':token}


def draft(client, headers, payment='cash'):
    assert client.post('/api/cart',headers=headers,json={'id':1,'delta':1}).status_code==200
    q = client.post('/api/quote',headers=headers,json={'method':'pickup'}).json()
    return {**q,'method':'pickup','payment':payment,'customer':'Покупатель','phone':'+79991234567','comment':'Без звонка в дверь','consent':True}


def test_web_checkout_snapshot_idempotency_and_no_telegram_customer(client):
    headers = session(client)
    data = draft(client,headers)
    first = client.post('/api/checkout',headers=headers,json=data)
    assert first.status_code==200,first.text
    second = client.post('/api/checkout',headers=headers,json=data)
    assert first.json()==second.json()
    assert client.get(first.json()['url']).status_code==200
    assert client.get('/order/1').status_code==404
    assert client.get('/api/cart').json()['quantity']==0
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM orders').fetchone()[0]==1
        o = c.execute('SELECT * FROM orders').fetchone()
        assert o['channel']=='web' and o['user_id']<0 and o['payment_status']=='unpaid'
        assert c.execute('SELECT count(*) FROM outbox WHERE chat_id<0').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM outbox WHERE chat_id=999').fetchone()[0]==1
    db.set_status(o['id'],'accepted')
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM outbox WHERE chat_id<0').fetchone()[0]==0


def test_checkout_reprices_and_stock_and_csrf(client):
    headers = session(client)
    assert client.post('/api/cart',json={'id':1,'delta':1}).status_code==403
    assert client.post('/api/cart',headers=headers,json={'id':1,'delta':100}).status_code==400
    data = draft(client,headers)
    with db.connect(True) as c:
        c.execute('UPDATE products SET price=price+100 WHERE id=1')
    result = client.post('/api/checkout',headers=headers,json=data)
    assert result.status_code==400 and 'изменились' in result.json()['detail']
    with db.connect(True) as c:
        c.execute('UPDATE products SET active=0 WHERE id=1')
    assert client.post('/api/quote',headers=headers,json={'method':'pickup'}).status_code==400


def test_client_prices_ignored_and_online_disabled(client):
    headers=session(client); data=draft(client,headers)
    data['total']=1;data['payment']='tbank'
    assert client.post('/api/checkout',headers=headers,json=data).status_code==400
    data['payment']='cash'
    assert client.post('/api/checkout',headers=headers,json=data).status_code==200
    with db.connect() as c:
        assert c.execute('SELECT total FROM orders').fetchone()[0]==59000


def test_public_does_not_expose_private_media_or_credentials(client):
    set_config('tbank',{'password':'TOP-SECRET-123','terminal':'123','enabled':False})
    page=client.get('/')
    assert page.status_code==200 and 'TOP-SECRET' not in page.text
    assert client.get('/assets/'+'a'*32+'.jpg').status_code==404
    assert client.get('/admin/integrations',follow_redirects=False).status_code==303
    token=login(client)
    page=client.get('/admin/integrations')
    assert 'TOP-SECRET' not in page.text and 'Сохранён' in page.text
    with db.connect() as c:
        assert 'TOP-SECRET' not in c.execute("SELECT config FROM integrations WHERE name='tbank'").fetchone()[0]
    assert get_config('tbank')['password']=='TOP-SECRET-123'
    for path in ('/admin/appearance','/admin/products/1','/admin/orders'):
        assert client.get(path).status_code==200


def test_appearance_carousel_and_safe_links(client):
    token=login(client)
    fields={'csrf':token,'title':'<script>bad</script>','subtitle':'Тест','button':'Меню','target':'javascript:alert(1)','active':'on'}
    assert 'Кнопка может вести' in client.post('/admin/slides',data=fields).text
    fields['target']='#catalog'
    assert 'Слайд сохранён' in client.post('/admin/slides',data=fields).text
    result=client.post('/admin/appearance',data={'csrf':token,'shop_name':'Мой магазин','tagline':'Тест','hero_mode':'carousel'})
    assert 'Оформление сохранено' in result.text
    page=client.get('/')
    assert 'Мой магазин' in page.text and 'data-slide="1"' in page.text
    assert '&lt;script&gt;bad' in page.text and '<script>bad' not in page.text


def test_cart_sessions_are_isolated(client):
    headers=session(client);draft(client,headers)
    cookies=dict(client.cookies)
    client.cookies.clear()
    session(client)
    assert client.get('/api/cart').json()['quantity']==0
    client.cookies.clear();client.cookies.update(cookies)
    assert client.get('/api/cart').json()['quantity']==1


def test_migration_repeat_preserves_existing_data(shop):
    db.init();db.init()
    assert len(db.products())==5
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM slides').fetchone()[0]==1


def test_concurrent_submission_creates_one_order(client):
    from concurrent.futures import ThreadPoolExecutor
    headers=session(client);data=draft(client,headers)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _: client.post('/api/checkout',headers=headers,json=data),range(4)))
    assert all(r.status_code==200 for r in results)
    assert len({r.json()['url'] for r in results})==1
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM orders').fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM outbox').fetchone()[0]==1


def test_legacy_database_migration(tmp_path, monkeypatch):
    import sqlite3
    from app import config
    from app.schema import migrate
    path=tmp_path/'legacy.sqlite3'
    monkeypatch.setattr(config,'DB',path)
    with sqlite3.connect(path) as c:
        c.executescript("""
          CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE products(id INTEGER PRIMARY KEY,name TEXT,price INTEGER);
          CREATE TABLE orders(id INTEGER PRIMARY KEY,token TEXT);
          CREATE TABLE order_items(id INTEGER PRIMARY KEY,order_id INTEGER,name TEXT);
          INSERT INTO products VALUES (1,'Существующее блюдо',73000);
          INSERT INTO orders VALUES (1,'existing-order');
          INSERT INTO order_items VALUES (1,1,'Снимок блюда');
        """)
    with db.connect(True) as c:migrate(c)
    with db.connect(True) as c:migrate(c)
    with db.connect() as c:
        assert c.execute('SELECT price FROM products WHERE id=1').fetchone()[0]==73000
        assert c.execute('SELECT channel FROM orders WHERE id=1').fetchone()[0]=='telegram'
        assert c.execute('SELECT name FROM order_items WHERE id=1').fetchone()[0]=='Снимок блюда'


def test_backup_preserves_vault_key(shop,tmp_path):
    import zipfile
    from app.manage import backup
    from cryptography.fernet import Fernet
    set_config('tbank',{'password':'protected-secret'})
    path=backup(tmp_path/'backups')
    with zipfile.ZipFile(path) as archive:
        key=archive.read('integrations.key')
        assert '.env' not in archive.namelist()
    with db.connect() as c:
        encrypted=c.execute("SELECT config FROM integrations WHERE name='tbank'").fetchone()[0]
    assert b'protected-secret' in Fernet(key).decrypt(encrypted.encode())
