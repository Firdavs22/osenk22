import asyncio
import io
import time

import httpx
from PIL import Image

from app import db, config, menu_sync as sync, menu_import as imp
from app.vault import get_config, set_config
from test_admin import login
from test_menu_import import CFG, PRODUCT, ORG, menu, batch


def imported():
    with db.connect() as c:
        return dict(c.execute('SELECT * FROM products WHERE iiko_id=?',(PRODUCT,)).fetchone())


def due():
    with db.connect(True) as c:
        c.execute('UPDATE menu_sync SET next_run=-1')


def test_scheduler_interval_category_move_and_no_duplicates(shop,monkeypatch):
    set_config('iiko',{**CFG,'auto_sync':True,'sync_minutes':5})
    data=menu();calls=[]
    async def api(cfg,path,body):
        calls.append(body)
        return data
    monkeypatch.setattr(imp,'iiko_call',api)
    asyncio.run(sync.sync_tick()); p=imported()
    assert p['active']==0 and p['category_id']
    asyncio.run(sync.sync_tick());assert len(calls)==1
    data['itemCategories'][0]['name']='Новая группа'
    data['itemCategories'][0]['items'][0]['itemSizes'][0]['prices'][0]['price']=800
    due();asyncio.run(sync.sync_tick())
    assert imported()['id']==p['id'] and imported()['price']==80000
    assert imported()['category_id']!=p['category_id']
    with db.connect() as c:
        assert c.execute('SELECT name FROM categories WHERE id=?',(imported()['category_id'],)).fetchone()[0]=='Новая группа'
        assert c.execute('SELECT last_success FROM menu_sync').fetchone()[0]
    assert len(db.products())==6


def test_disabled_manual_run_and_lease_exclusion(shop,monkeypatch):
    set_config('iiko',CFG);calls=[]
    async def api(*args):
        calls.append(1)
        await asyncio.sleep(.01)
        return menu()
    monkeypatch.setattr(imp,'iiko_call',api)
    asyncio.run(sync.sync_tick()); assert calls==[]
    due()
    async def concurrent(): await asyncio.gather(sync.sync_tick(),sync.sync_tick())
    asyncio.run(concurrent()); assert len(calls)==1
    due()
    with db.connect(True) as c:
        c.execute("UPDATE menu_sync SET owner='dead-process',lease_until=?",(time.time()-1,))
    asyncio.run(sync.sync_tick()); assert len(calls)==2


def test_empty_error_and_changed_config_leave_catalog_untouched(shop,monkeypatch):
    set_config('iiko',CFG);imp.apply_import(batch(menu()))
    before=[dict(p) for p in db.products()]
    for kind in ('empty','http','changed'):
        async def api(*args):
            data=menu()
            if kind=='empty':data['itemCategories']=[]
            elif kind=='http':
                httpx.Response(503,request=httpx.Request('POST','https://api-ru.iiko.services/api/2/menu/by_id')).raise_for_status()
            else:set_config('iiko',{**CFG,'external_menu':'other'})
            return data
        set_config('iiko',CFG);monkeypatch.setattr(imp,'iiko_call',api);due()
        asyncio.run(sync.sync_tick())
        assert [dict(p) for p in db.products()]==before
        with db.connect() as c:
            state=c.execute('SELECT * FROM menu_sync').fetchone()
            assert state['error'] and state['lease_until']==0


def test_bulk_csrf_visibility_and_auto_return(client):
    assert client.post('/admin/products/bulk',data={'action':'publish_all'},follow_redirects=False).status_code==303
    csrf=login(client);set_config('iiko',CFG);imp.apply_import(batch(menu()));p=imported()
    assert client.post('/admin/products/bulk',data={'action':'publish_all'}).status_code==403
    client.post('/admin/products/bulk',data={'csrf':csrf,'action':'publish','product_id':p['id']})
    assert imported()['active']==1
    hidden=menu();hidden['itemCategories'][0]['items'][0]['isHidden']=True
    imp.apply_import(batch(hidden));assert imported()['active']==0 and imported()['iiko_available']==0
    client.post('/admin/products/bulk',data={'csrf':csrf,'action':'publish_all'})
    assert imported()['active']==0
    imp.apply_import(batch(menu()));assert imported()['active']==1
    client.post('/admin/products/bulk',data={'csrf':csrf,'action':'hide','product_id':p['id']})
    imp.apply_import(batch(menu()));assert imported()['active']==0
    imp.apply_import(batch(hidden));imp.apply_import(batch(menu()));assert imported()['active']==0
    assert 'Опубликовать выбранные' in client.get('/admin/products').text


def test_settings_preserve_credentials_and_queue(client):
    csrf=login(client);set_config('iiko',CFG)
    for route in ('sync-settings','sync-now','photos-retry'):
        assert client.post('/admin/integrations/iiko/'+route,data={'csrf':'bad'}).status_code==403
    page=client.post('/admin/integrations/iiko/sync-settings',data={'csrf':csrf,'auto_sync':'on','sync_minutes':'5'})
    assert page.status_code==200 and 'Автоматическое обновление меню' in page.text
    assert get_config('iiko')['api_key']==CFG['api_key'] and get_config('iiko')['auto_sync']
    client.post('/admin/integrations/iiko/sync-settings',data={'csrf':csrf,'sync_minutes':'0'})
    assert get_config('iiko')['auto_sync']
    client.post('/admin/integrations/iiko/sync-now',data={'csrf':csrf})
    with db.connect() as c:assert c.execute('SELECT next_run FROM menu_sync').fetchone()[0]==-1


def test_photo_refresh_manual_override_and_failure_keeps_previous(shop,monkeypatch):
    set_config('iiko',CFG);imp.apply_import(batch(menu()))
    blob=io.BytesIO();Image.new('RGB',(64,64),'red').save(blob,'PNG')
    status=[200]
    class Response:
        @property
        def status_code(self):return status[0]
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def aiter_bytes(self):yield blob.getvalue()
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        def stream(self,*args,**kwargs):return Response()
    monkeypatch.setattr(imp.httpx,'AsyncClient',Client)
    asyncio.run(imp.image_tick());first=imported()['photo']
    assert first and first==imported()['iiko_photo']
    imp.apply_import(batch(menu()));asyncio.run(imp.image_tick());second=imported()['photo']
    assert second!=first and not (config.MEDIA/first).exists()
    imp.apply_import(batch(menu()));status[0]=503
    for _ in range(3):asyncio.run(imp.image_tick())
    assert imported()['photo']==second and (config.MEDIA/second).exists()
    with db.connect(True) as c:
        job=c.execute('SELECT * FROM menu_images').fetchone()
        assert job['state']=='failed' and '503' in job['error']
        c.execute("UPDATE products SET photo='own.jpg' WHERE iiko_id=?",(PRODUCT,))
    assert imp.apply_import(batch(menu()))['photos']==0
    assert imported()['photo']=='own.jpg'


def test_unsupported_photo_is_diagnosable_and_not_requested(shop,monkeypatch):
    set_config('iiko',CFG);data=menu()
    data['itemCategories'][0]['items'][0]['itemSizes'][0]['buttonImageUrl']='https://unsupported.example/image.jpg'
    assert imp.apply_import(batch(data))['photos']==0
    with db.connect() as c:
        job=c.execute('SELECT * FROM menu_images').fetchone()
        assert job['state']=='failed' and 'unsupported.example' in job['error']
    async def unexpected(*args):raise AssertionError('No network allowed')
    monkeypatch.setattr(imp,'load_image',unexpected)
    asyncio.run(imp.image_tick())


def test_stale_photo_download_cannot_replace_new_job_or_manual_photo(shop,monkeypatch):
    set_config('iiko',CFG);imp.apply_import(batch(menu()))
    blob=io.BytesIO();Image.new('RGB',(64,64),'red').save(blob,'PNG')
    class Response:
        status_code=200
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def aiter_bytes(self):
            # A newer menu arrives while the previous image is downloading.
            changed=menu()
            changed['itemCategories'][0]['items'][0]['itemSizes'][0]['buttonImageUrl']='https://102922.selcdn.ru/new.jpg'
            imp.apply_import(batch(changed))
            yield blob.getvalue()
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        def stream(self,*args,**kwargs):return Response()
    monkeypatch.setattr(imp.httpx,'AsyncClient',Client)
    asyncio.run(imp.image_tick())
    assert not imported()['photo']
    with db.connect() as c:
        job=c.execute('SELECT * FROM menu_images').fetchone()
        assert job['state']=='pending' and job['url'].endswith('/new.jpg')


def test_photo_retry_route_recovers_supported_storage(client):
    csrf=login(client);set_config('iiko',CFG);imp.apply_import(batch(menu()))
    with db.connect(True) as c:
        c.execute("UPDATE menu_images SET state='failed',attempts=3,error='HTTP 503'")
    client.post('/admin/integrations/iiko/photos-retry',data={'csrf':csrf})
    with db.connect() as c:
        job=c.execute('SELECT * FROM menu_images').fetchone()
        assert job['state']=='pending' and job['attempts']==0 and not job['error']


def test_unchanged_photo_uses_conditional_request_and_changed_url_drops_cache(shop,monkeypatch):
    set_config('iiko',CFG);imp.apply_import(batch(menu()))
    blob=io.BytesIO();Image.new('RGB',(64,64),'red').save(blob,'PNG')
    seen=[]
    class Response:
        headers={'etag':'"image-v1"','last-modified':'Sun, 27 Sep 2026 10:00:00 GMT'}
        def __init__(self,status):self.status_code=status
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def aiter_bytes(self):
            assert self.status_code==200
            yield blob.getvalue()
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        def stream(self,method,url,headers):
            seen.append(headers)
            return Response(304 if headers.get('If-None-Match') else 200)
    monkeypatch.setattr(imp.httpx,'AsyncClient',Client)
    asyncio.run(imp.image_tick());first=imported()['photo']
    imp.apply_import(batch(menu()));asyncio.run(imp.image_tick())
    assert imported()['photo']==first and seen[1]['If-None-Match']=='"image-v1"'
    assert (config.MEDIA/first).is_file()
    changed=menu();changed['itemCategories'][0]['items'][0]['itemSizes'][0]['buttonImageUrl']='https://102922.selcdn.ru/replacement.jpg'
    imp.apply_import(batch(changed));asyncio.run(imp.image_tick())
    assert seen[2]=={} and imported()['photo']!=first
