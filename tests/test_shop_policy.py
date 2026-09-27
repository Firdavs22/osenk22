import copy
import json

import pytest

from app import db, menu_import as imp
from app.shop_policy import apply_profile, documents, DISTRICTS
from app.integrations import iiko_payload
from app.vault import set_config
from test_store import session, draft
from test_admin import login
from test_menu_import import menu, CFG, ORG
from test_integrations import configure_bank


def test_pickup_discount_matches_receipt_and_iiko(client):
    apply_profile(); configure_bank()
    with db.connect(True) as c:
        c.execute('UPDATE products SET price=10001,iiko_id=? WHERE id=1',(ORG,))
    headers=session(client)
    data=draft(client,headers,'tbank')
    result=client.post('/api/checkout',headers=headers,json=data)
    assert result.status_code==200,result.text
    with db.connect() as c:
        order=dict(c.execute('SELECT * FROM orders').fetchone())
        receipt=json.loads(c.execute('SELECT receipt FROM payments').fetchone()[0])
    assert order['subtotal']==10001 and order['discount']==1000 and order['total']==9001
    assert sum(p['Amount'] for p in receipt['Items'])==order['total']
    payload=iiko_payload({**order,'order_id':order['id'],'external_id':ORG}, {'organization_id':ORG,'terminal_group':ORG})
    assert sum(round(p['price']*100)*p['amount'] for p in payload['order']['items'])==order['total']
    assert 'Петербургская' in order['legal_snapshot']
    assert client.post('/api/checkout',headers=headers,json=data).json()==result.json()


@pytest.mark.parametrize('amount,fee',[(139999,30000),(140000,0),(140001,0)])
def test_delivery_threshold_and_area_cannot_be_forged(shop,amount,fee):
    apply_profile()
    with db.connect(True) as c:c.execute('UPDATE products SET price=? WHERE id=1',(amount,))
    db.cart_change(10,1,1)
    for district in DISTRICTS:
        q=db.quote(10,{'method':'delivery','district':district,'delivery':0,'discount':100000})
        assert q['delivery']==fee and q['discount']==0 and q['total']==amount+fee
    for district in ('','outside','Другой'):
        with pytest.raises(ValueError,match='стоимость нужно согласовать'):
            db.quote(10,{'method':'delivery','district':district})


def test_payment_at_door_accepted_and_terms_change_requires_reconfirmation(client):
    apply_profile(); headers=session(client);data=draft(client,headers,'card')
    with db.connect(True) as c:c.execute("UPDATE settings SET value='Новая оферта' WHERE key='offer_text'")
    assert client.post('/api/checkout',headers=headers,json=data).status_code==400
    data.update(client.post('/api/quote',headers=headers,json={'method':'pickup'}).json())
    result=client.post('/api/checkout',headers=headers,json=data)
    assert result.status_code==200
    with db.connect() as c:oid=c.execute('SELECT id FROM orders').fetchone()[0]
    db.set_status(oid,'accepted')
    assert 'картой' in client.get(result.json()['url']).text
    assert documents(db.settings())['offer']=='Новая оферта'


def test_all_skipped_shows_reasons_without_mutation_and_diagnostic_is_redacted(client,monkeypatch):
    csrf=login(client);set_config('iiko',CFG)
    data=menu();data['secret']='must-not-leak'
    item=data['itemCategories'][0]['items'][0]
    item['itemSizes'][0]['prices'][0]['organizations']=['other-organization']
    item['itemSizes'][0]['prices'][0]['apiKey']='must-not-leak'
    async def api(*args,**kwargs):return data
    monkeypatch.setattr(imp,'iiko_call',api)
    before=[dict(p) for p in db.products()]
    page=client.post('/admin/integrations/iiko/menu-preview',data={'csrf':csrf})
    assert 'нет записи цены' in page.text and 'Применить импорт' not in page.text
    assert [dict(p) for p in db.products()]==before
    assert client.post('/admin/integrations/iiko/menu-diagnostic',data={'csrf':'wrong'}).status_code==403
    report=client.post('/admin/integrations/iiko/menu-diagnostic',data={'csrf':csrf})
    assert report.status_code==200 and 'attachment' in report.headers['content-disposition']
    assert 'must-not-leak' not in report.text and 'client_secret' not in report.text
    assert report.json()['counts']['items']==1


def test_optional_modifiers_and_divisible_whole_portions():
    data=menu();item=data['itemCategories'][0]['items'][0];item['canBeDivided']=True
    size=item['itemSizes'][0]
    size['itemModifierGroups']=[{'restrictions':{'minQuantity':0,'byDefault':0},'items':[
        {'restrictions':[{'minQuantity':0,'byDefault':0}]}]}]
    assert len(imp.normalize_menu(data,CFG)['rows'])==1
    for restriction in ({'minQuantity':1}, {'minQuantity':0,'byDefault':1}, None):
        changed=copy.deepcopy(data)
        changed['itemCategories'][0]['items'][0]['itemSizes'][0]['itemModifierGroups'][0]['restrictions']=restriction
        with pytest.raises(ValueError,match='модификаторы'):
            imp.normalize_menu(changed,CFG)


def test_food_information_and_footer_legal_pages(client):
    apply_profile()
    with db.connect(True) as c:
        c.execute("UPDATE products SET nutrition='На 100 г: 200 ккал',allergens='Рыба',storage='Условия по маркировке' WHERE id=1")
    page=client.get('/')
    assert '200 ккал' in page.text and '165510330630' in page.text
    assert 'согласен на' not in page.text
    for name in ('offer','privacy','returns','contacts','delivery'):
        assert client.get('/legal/'+name).status_code==200
