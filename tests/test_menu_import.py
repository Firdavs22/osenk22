import asyncio
import copy
import io
import json
import re
import secrets
import time

import pytest
from PIL import Image

from app import config, db, menu_import as imp
from app.vault import set_config
from test_admin import login

ORG='11111111-1111-4111-8111-111111111111'
PRODUCT='22222222-2222-4222-8222-222222222222'
SIZE='33333333-3333-4333-8333-333333333333'
CFG={'organization_id':ORG,'external_menu':'123','price_category':'','enabled':False,
     'api_key':'secret','app_id':ORG,'client_secret':'secret2'}


def menu():
    return {'id':123,'name':'Меню доставки','formatVersion':2,'itemCategories':[{'id':'cat1','name':'Роллы iiko','items':[
        {'itemId':PRODUCT,'name':'Филадельфия iiko','description':'Нежный вкус','orderItemType':'Product','type':'DISH',
         'labels':[{'name':'Хит'}],'allergens':[{'name':'Рыба'}],
         'itemSizes':[{'sizeId':SIZE,'sizeName':'8 шт.','portionWeightGrams':280,'itemModifierGroups':[],
                       'prices':[{'organizations':[ORG],'price':650.5}],
                       'buttonImageUrl':'https://102922.selcdn.ru/ecomm/photo.png'}]}]}]}


def batch(data):
    with db.connect(True) as c:
        payload=imp.prepare_preview(c,imp.normalize_menu(data,CFG));token=secrets.token_urlsafe(32)
        c.execute('INSERT INTO menu_imports(token,payload,fingerprint,created) VALUES (?,?,?,?)',
                  (token,json.dumps(payload),imp.catalog_fingerprint(c),time.time()))
        return token


def test_import_drafts_repeat_prices_and_preserves_editorial_fields(shop):
    set_config('iiko',CFG)
    token=batch(menu());result=imp.apply_import(token)
    assert result['created']==1 and result['photos']==1
    assert imp.apply_import(token)==result
    with db.connect(True) as c:
        p=dict(c.execute('SELECT * FROM products WHERE iiko_id=?',(PRODUCT,)).fetchone())
        assert p['active']==0 and p['ingredients']=='' and p['price']==65050 and p['weight']=='280 г'
        assert 'Аллергены' in p['description'] and p['iiko_size']==SIZE
        c.execute("UPDATE products SET ingredients='Проверенный состав',description='Своё описание',photo='own.jpg',active=1 WHERE id=?",(p['id'],))
    data=menu();data['itemCategories'][0]['items'][0]['itemSizes'][0]['prices'][0]['price']=700
    result=imp.apply_import(batch(data))
    assert result['created']==0 and result['updated']==1 and result['photos']==0
    updated=db.product(p['id'])
    assert updated['price']==70000 and updated['active']==1
    assert updated['ingredients']=='Проверенный состав' and updated['description']=='Своё описание' and updated['photo']=='own.jpg'
    assert len(db.products())==6


def test_stale_preview_and_different_source_cannot_overwrite(shop):
    set_config('iiko',CFG);token=batch(menu())
    with db.connect(True) as c:c.execute("UPDATE products SET name='Правка сотрудника' WHERE id=1")
    with pytest.raises(ValueError,match='Каталог изменился'):imp.apply_import(token)
    token=batch(menu());set_config('iiko',{**CFG,'external_menu':'999'})
    with pytest.raises(ValueError,match='Настройки меню изменились'):imp.apply_import(token)
    assert len(db.products())==5


def test_removed_and_hidden_only_affect_managed_menu(shop):
    set_config('iiko',CFG);imp.apply_import(batch(menu()))
    with db.connect(True) as c:c.execute('UPDATE products SET active=1 WHERE iiko_id=?',(PRODUCT,))
    data=menu();data['itemCategories'][0]['items'][0]['isHidden']=True
    imp.apply_import(batch(data))
    with db.connect() as c:assert c.execute('SELECT active FROM products WHERE iiko_id=?',(PRODUCT,)).fetchone()[0]==0
    # Removing one UUID from a non-empty menu hides it without removing order history.
    with db.connect(True) as c:c.execute('UPDATE products SET active=1 WHERE iiko_id=?',(PRODUCT,))
    data=menu();data['itemCategories'][0]['items'][0]['itemId']=ORG
    result=imp.apply_import(batch(data))
    assert result['hidden']==1
    assert db.product(1)['active']==1
    with db.connect() as c:assert c.execute('SELECT active FROM products WHERE iiko_id=?',(PRODUCT,)).fetchone()[0]==0


def test_duplicate_and_ambiguous_menu_rejected_and_unsupported_skipped():
    data=menu();data['itemCategories'][0]['items'].append(copy.deepcopy(data['itemCategories'][0]['items'][0]))
    with pytest.raises(ValueError,match='повторяются'):imp.normalize_menu(data,CFG)
    data=menu();data['formatVersion']=3
    with pytest.raises(ValueError,match='формат'):imp.normalize_menu(data,CFG)
    data=menu();data['itemCategories'][0]['items'][0]['itemSizes'][0]['prices'][0]['organizations']=[SIZE]
    with pytest.raises(ValueError,match='Нет поддерживаемых'):imp.normalize_menu(data,CFG)
    data=menu();unsupported=copy.deepcopy(data['itemCategories'][0]['items'][0]);unsupported['itemId']=ORG
    unsupported['itemSizes'][0]['itemModifierGroups']=[{'name':'Обязательный соус'}]
    data['itemCategories'][0]['items'].append(unsupported)
    parsed=imp.normalize_menu(data,CFG)
    assert len(parsed['rows'])==1 and 'модификаторы' in parsed['skipped'][0]


def test_routes_require_admin_csrf_and_explicit_apply(client,monkeypatch):
    assert client.post('/admin/integrations/iiko/menu-preview',follow_redirects=False).status_code==303
    csrf=login(client);set_config('iiko',CFG)
    async def api(cfg,path,body,token=None):
        assert path=='/api/2/menu/by_id' and body['version']==2 and body['organizationIds']==[ORG]
        return menu()
    monkeypatch.setattr(imp,'iiko_call',api)
    assert client.post('/admin/integrations/iiko/menu-preview',data={'csrf':'bad'}).status_code==403
    page=client.post('/admin/integrations/iiko/menu-preview',data={'csrf':csrf})
    assert page.status_code==200 and 'Применить импорт' in page.text
    assert len(db.products())==5
    token=re.search(r'name="import_token" value="([^"]+)"',page.text).group(1)
    result=client.post('/admin/integrations/iiko/menu-apply',data={'csrf':csrf,'import_token':token})
    assert 'создано 1' in result.text and len(db.products())==6


@pytest.mark.parametrize('url',['http://102922.selcdn.ru/a','https://127.0.0.1/a','https://evil.test/a',
    'https://102922.selcdn.ru.evil.test/a','https://user:pass@102922.selcdn.ru/a','https://102922.selcdn.ru:8000/a'])
def test_image_source_restrictions(url):
    assert not imp.photo_allowed(url)


def test_image_worker_validates_and_saves_local_jpeg(shop,monkeypatch):
    set_config('iiko',CFG);imp.apply_import(batch(menu()))
    blob=io.BytesIO();Image.new('RGB',(64,64),'red').save(blob,'PNG')
    class Response:
        status_code=200
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def aiter_bytes(self):yield blob.getvalue()
    class Client:
        def __init__(self,**kwargs):assert kwargs['follow_redirects'] is False
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        def stream(self,method,url):return Response()
    monkeypatch.setattr(imp.httpx,'AsyncClient',Client)
    asyncio.run(imp.image_tick())
    with db.connect() as c:
        photo=c.execute('SELECT photo FROM products WHERE iiko_id=?',(PRODUCT,)).fetchone()[0]
        assert c.execute('SELECT state FROM menu_images').fetchone()[0]=='done'
    assert photo.endswith('.jpg')
    with Image.open(config.MEDIA/photo) as image:assert image.format=='JPEG'
