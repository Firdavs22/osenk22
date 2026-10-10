import asyncio
import uuid

import pytest

from app import db, operations, integrations, menu_import as imp
from app.vault import get_config, set_config
from test_admin import login
from test_store import session, draft
from test_integrations import create_online
from test_max_chat import Chat
from test_menu_import import CFG, menu, batch


def enable_auto():
    with db.connect(True) as c:c.execute("UPDATE settings SET value='1' WHERE key='auto_accept'")
    set_config('iiko',{'enabled':True})


def test_auto_accept_all_channels_and_no_retroactive_changes(client):
    # Existing pending orders are left for the staff; only new submissions auto-accept.
    Chat().order();enable_auto();Chat(777,777).order()
    headers=session(client);data=draft(client,headers)
    client.post('/api/checkout',headers=headers,json=data)
    client.post('/api/checkout',headers=headers,json=data)
    db.cart_change(100,1,1)
    data={'method':'pickup','customer':'Тест','phone':'+79991234567','address':'Адрес','comment':'','token':'tg-order'}
    data['fingerprint']=db.quote(100,data)['fingerprint']
    db.save_draft(100,'confirm',data);db.place_order(100,'tg-order');db.place_order(100,'tg-order')
    with db.connect() as c:
        assert [r[0] for r in c.execute('SELECT status FROM orders ORDER BY id')]==['new','accepted','accepted','accepted']
        assert c.execute('SELECT count(*) FROM iiko_jobs').fetchone()[0]==3
    db.init()
    with db.connect() as c:assert c.execute('SELECT status FROM orders WHERE id=1').fetchone()[0]=='new'


def test_online_auto_accept_only_verified_payment_and_once(client):
    enable_auto();oid=create_online(client)
    with db.connect(True) as c:
        assert c.execute('SELECT status FROM orders WHERE id=?',(oid,)).fetchone()[0]=='new'
        assert not c.execute('SELECT 1 FROM iiko_jobs').fetchone()
        c.execute("UPDATE payments SET payment_id='p1' WHERE order_id=?",(oid,))
    row=integrations.payment_row(oid)
    result={'TerminalKey':'test-terminal','PaymentId':'p1','Amount':row['total'],'OrderId':row['bank_order'],'Status':'AUTHORIZED'}
    integrations.apply_payment(row,result)
    with db.connect() as c:assert c.execute('SELECT status FROM orders WHERE id=?',(oid,)).fetchone()[0]=='new'
    result['Status']='CONFIRMED';integrations.apply_payment(row,result);integrations.apply_payment(row,result)
    with db.connect() as c:
        assert c.execute('SELECT status FROM orders WHERE id=?',(oid,)).fetchone()[0]=='accepted'
        assert c.execute('SELECT count(*) FROM iiko_jobs').fetchone()[0]==1
    # A duplicate bank callback must not auto-accept an already paid legacy order.
    with db.connect(True) as c:c.execute("UPDATE orders SET status='new' WHERE id=?",(oid,))
    integrations.apply_payment(row,result)
    with db.connect() as c:assert c.execute('SELECT status FROM orders WHERE id=?',(oid,)).fetchone()[0]=='new'


def test_kanban_modal_auth_transitions_and_escaping(client):
    Chat().order()
    assert client.get('/admin/orders/1?fragment=true',follow_redirects=False).status_code==303
    token=login(client)
    with db.connect(True) as c:c.execute("UPDATE orders SET customer='<script>alert(1)</script>' WHERE id=1")
    page=client.get('/admin/orders');assert 'orders-kanban' in page.text and 'order-modal' in page.text
    assert '<script>alert(1)</script>' not in page.text and '&lt;script&gt;' in page.text
    detail=client.get('/admin/orders/1?fragment=true')
    assert 'data-order-detail' in detail.text and '<html' not in detail.text and 'Филадельфия' in detail.text
    path='/admin/orders/1/status';headers={'Accept':'application/json'}
    assert client.post(path,headers=headers,data={'status':'accepted'}).status_code==403
    assert client.post(path,headers=headers,data={'csrf':token,'status':'accepted'}).json()=={'ok':True}
    assert client.post(path,headers=headers,data={'csrf':token,'status':'accepted'}).status_code==400
    assert 'data-order="1"' not in client.get('/admin/orders?status=new&fragment=true').text
    assert 'data-order="1"' in client.get('/admin/orders?status=accepted&fragment=true').text


def test_telegram_admin_add_preserves_credentials_and_existing_recipients(shop,capsys):
    set_config('telegram',{'token':'secret','admin_ids':[17],'enabled':True,'mini_app':True})
    operations.telegram_admin_add(42);operations.telegram_admin_add(42)
    cfg=get_config('telegram')
    assert cfg['admin_ids']==[17,42] and cfg['token']=='secret' and cfg['mini_app']
    assert 'secret' not in capsys.readouterr().out
    with pytest.raises(ValueError):operations.telegram_admin_add(-1)


def test_menu_switch_preview_and_apply_preserve_shared_cards(shop,monkeypatch):
    set_config('iiko',CFG);imp.apply_import(batch(menu()))
    with db.connect(True) as c:
        c.execute("UPDATE products SET active=1,ingredients='Мой состав' WHERE iiko_source=?",(imp.source_key(CFG),))
        c.execute("INSERT INTO products(category_id,name,ingredients,price,iiko_id,iiko_source) VALUES (1,'Старое блюдо','Рис',100,?,?)",(str(uuid.uuid4()),imp.source_key(CFG)))
    target=menu();target['id']=456;target['name']='Новое меню';target['itemCategories'][0]['name']='Новая группа'
    target['itemCategories'][0]['items'][0]['itemSizes'][0]['prices'][0]['price']=700
    async def call(cfg,path,body):
        return {'externalMenus':[{'id':'456','name':'Новое меню'}],'priceCategories':[]} if path=='/api/2/menu' else target
    monkeypatch.setattr(operations,'iiko_call',call)
    asyncio.run(operations.iiko_menus('456'))
    assert get_config('iiko')['external_menu']=='123'
    asyncio.run(operations.iiko_menus('456',apply=True))
    assert get_config('iiko')['external_menu']=='456'
    with db.connect() as c:
        shared=c.execute("SELECT * FROM products WHERE name LIKE 'Филадельфия iiko%'").fetchone()
        assert shared['active']==1 and shared['ingredients']=='Мой состав' and shared['price']==70000
        assert c.execute("SELECT active FROM products WHERE name='Старое блюдо'").fetchone()[0]==0
        assert c.execute('SELECT active FROM products WHERE id=1').fetchone()[0]==1  # Manual catalog untouched.
    async def empty(cfg,path,body):
        return {'externalMenus':[{'id':'123'}]} if path=='/api/2/menu' else {'itemCategories':[]}
    monkeypatch.setattr(operations,'iiko_call',empty)
    with pytest.raises(ValueError):asyncio.run(operations.iiko_menus('123',apply=True))
    assert get_config('iiko')['external_menu']=='456'
