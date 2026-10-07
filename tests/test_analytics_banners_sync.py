import asyncio
import io
import json
import uuid

from PIL import Image
from app import db, reporting, iiko_status, integrations
from app.vault import seal
from test_admin import login
from test_store import session, draft


def place(client, headers=None):
    headers = headers or session(client)
    return client.post('/api/checkout',headers=headers,json=draft(client,headers)).json()['url']


def test_attribution_first_last_unicode_expiry_and_checkout_retry(client):
    headers = session(client)
    client.get('/',params={k:'Я'*100 for k in reporting.UTM})
    client.get('/?utm_source=yandex&utm_campaign=autumn')
    client.get('/')
    data = draft(client,headers)
    result = client.post('/api/checkout',headers=headers,json=data)
    assert result.status_code==200
    assert client.post('/api/checkout',headers=headers,json=data).json()==result.json()
    with db.connect() as c:
        row=c.execute('SELECT * FROM order_attribution').fetchone()
        assert json.loads(row['first_touch'])['utm_source']=='Я'*100
        assert json.loads(row['last_touch'])['utm_campaign']=='autumn'
        assert c.execute('SELECT count(*) FROM order_attribution').fetchone()[0]==1
        assert c.execute('SELECT category_name FROM order_items').fetchone()[0]
    assert len(client.cookies.get('sushi_store'))<2000
    with db.connect(True) as c:c.execute('UPDATE visitor_attribution SET updated=0')
    place(client,headers)
    with db.connect() as c:assert c.execute('SELECT count(*) FROM order_attribution').fetchone()[0]==1


def test_reports_moscow_bounds_currency_repeat_and_cancelled(client):
    for _ in range(4):place(client)
    with db.connect(True) as c:
        c.execute("UPDATE orders SET created_at='2026-10-06 21:00:00',status='done',total=10000 WHERE id=1")
        c.execute("UPDATE orders SET created_at='2026-10-07 20:59:59',status='cancelled',total=90000 WHERE id=2")
        c.execute("UPDATE orders SET created_at='2026-10-07 21:00:00',status='done',total=30000 WHERE id=3")
        c.execute("UPDATE orders SET created_at='2026-10-07 10:00:00',status='done',currency='USD',total=100 WHERE id=4")
    r=reporting.report({'start':'2026-10-07','end':'2026-10-07'})
    assert (r['n'],r['total'],r['average'],r['paid'])==(2,10000,10000,0)
    assert r['daily'][0]['amount']==10000 and r['counts']['cancelled']==1
    assert r['first']==1 and r['repeated']==0
    assert client.get('/admin/analytics',follow_redirects=False).status_code==303
    csrf=login(client)
    assert client.get('/admin/analytics?start=2026-10-07&end=2026-10-07').status_code==200
    assert 'Укажите даты' in client.get('/admin/analytics?start=bad').text
    assert client.post('/admin/analytics/settings',data={'metrika_id':'123'}).status_code==403
    client.post('/admin/analytics/settings',data={'csrf':csrf,'metrika_enabled':'on','metrika_id':'123','iiko_status_sync':'on'})
    assert db.settings()['metrika_enabled']=='1'
    assert 'https://mc.yandex.ru' in client.get('/').headers['content-security-policy']
    assert 'https://mc.yandex.ru' not in client.get('/admin').headers['content-security-policy']
    client.post('/admin/analytics/settings',data={'csrf':csrf,'metrika_enabled':'on','metrika_id':'<script>'})
    assert db.settings()['metrika_id']=='123'


def photo(color):
    out=io.BytesIO();Image.new('RGB',(80,100),color).save(out,format='PNG');return out.getvalue()


def test_background_banners_mobile_images_validation_and_visibility(client):
    csrf=login(client)
    form={'csrf':csrf,'title':'Роллы каждый день','subtitle':'Описание','button':'В меню','target':'#catalog',
          'layout':'background','text_align':'right','text_color':'#ffffff','button_color':'#000000',
          'button_text_color':'#ffffff','overlay':'40','active':'on','show_button':'on','show_text':'on'}
    result=client.post('/admin/slides',data=form,files={'photo':('a.png',photo('red'),'image/png'),'mobile_photo':('b.png',photo('blue'),'image/png')})
    assert result.status_code==200
    with db.connect() as c:slide=dict(c.execute('SELECT * FROM slides ORDER BY id DESC').fetchone())
    assert slide['layout']=='background' and slide['mobile_photo']!=slide['photo']
    assert client.get('/assets/'+slide['mobile_photo']).status_code==200
    assert '--banner-shade:0.4' in client.get('/banner-styles.css').text
    client.post('/admin/slides',data=form|{'id':slide['id'],'text_color':'invalid'})
    with db.connect() as c:assert c.execute('SELECT text_color FROM slides WHERE id=?',(slide['id'],)).fetchone()[0]=='#ffffff'
    form.pop('show_text');form['id']=slide['id'];form['active']=''
    client.post('/admin/slides',data=form)
    assert client.get('/assets/'+slide['mobile_photo']).status_code==404
    form['active']='on';client.post('/admin/slides',data=form)
    with db.connect() as c:assert c.execute('SELECT show_text FROM slides WHERE id=?',(slide['id'],)).fetchone()[0]==0
    db.init()  # repeated additive migrations must preserve settings and content
    with db.connect() as c:assert c.execute('SELECT count(*) FROM slides').fetchone()[0]==2


def job_for_order():
    external=str(uuid.uuid4())
    with db.connect(True) as c:
        c.execute("UPDATE orders SET status='accepted',channel='telegram',user_id=101 WHERE id=1")
        c.execute("INSERT INTO iiko_jobs(order_id,external_id,state,credentials) VALUES (1,?,'sent',?)",(external,seal({'organization_id':'org','api_key':'secret'})))
    return external


def remote(external,status):
    return {'orders':[{'id':external,'creationStatus':'Success','order':{'status':status}}]}


def test_iiko_poll_ready_skip_intermediate_notify_once_no_rollback(client,monkeypatch):
    place(client);external=job_for_order();calls=[];status='CookingCompleted'
    async def call(cfg,path,body):
        calls.append((path,body));return remote(external,status)
    monkeypatch.setattr(integrations,'iiko_call',call)
    asyncio.run(iiko_status.tick())
    with db.connect() as c:
        assert c.execute('SELECT status FROM orders WHERE id=1').fetchone()[0]=='ready'
        assert c.execute('SELECT count(*) FROM outbox WHERE chat_id=101').fetchone()[0]==1
        assert c.execute('SELECT sync_status FROM iiko_jobs').fetchone()[0]=='CookingCompleted'
    asyncio.run(iiko_status.tick());assert len(calls)==1
    for status in ['CookingCompleted','CookingStarted','UnknownStatus','Closed']:
        with db.connect(True) as c:c.execute('UPDATE iiko_jobs SET sync_next=0')
        asyncio.run(iiko_status.tick())
    with db.connect() as c:
        assert c.execute('SELECT status FROM orders WHERE id=1').fetchone()[0]=='done'
        assert c.execute('SELECT count(*) FROM outbox WHERE chat_id=101').fetchone()[0]==2
        assert c.execute('SELECT count(*) FROM iiko_jobs').fetchone()[0]==1
    assert all(path=='/api/1/deliveries/by_id' for path,_ in calls)
    asyncio.run(iiko_status.tick());assert len(calls)==5


def test_iiko_timeout_wrong_id_unpaid_terminal_protection_and_disable(client,monkeypatch):
    place(client);external=job_for_order()
    async def fail(*args):raise TimeoutError('secret data must not be saved')
    monkeypatch.setattr(integrations,'iiko_call',fail)
    asyncio.run(iiko_status.tick())
    with db.connect(True) as c:
        job=dict(c.execute('SELECT * FROM iiko_jobs').fetchone())
        assert 'secret' not in job['sync_error']
        import pytest
        with pytest.raises(ValueError):iiko_status.apply(c,job,remote('wrong','CookingCompleted'))
        c.execute("UPDATE orders SET payment_method='tbank',payment_status='pending' WHERE id=1")
        with pytest.raises(ValueError):iiko_status.apply(c,job,remote(external,'CookingCompleted'))
        assert c.execute('SELECT status FROM orders').fetchone()[0]=='accepted'
        c.execute("UPDATE orders SET status='cancelled' WHERE id=1")
        iiko_status.apply(c,job,remote(external,'Closed'))
        assert c.execute('SELECT status FROM orders').fetchone()[0]=='cancelled'
        c.execute("UPDATE settings SET value='0' WHERE key='iiko_status_sync'")
    asyncio.run(iiko_status.tick())


def test_metrika_order_data_owned_no_contacts_and_paid_truth(client):
    headers=session(client);url=place(client,headers)
    page=client.get(url).text
    import re
    data=json.loads(re.search("data-order='([^']+)'",page).group(1))
    assert data['purchase'] and not data['paid']
    assert set(data)=={'id','revenue','paid','cancelled','purchase','currency','products'}
    with db.connect(True) as c:c.execute("UPDATE orders SET payment_method='tbank',payment_status='pending' WHERE id=1")
    data=json.loads(re.search("data-order='([^']+)'",client.get(url).text).group(1))
    assert not data['purchase'] and not data['paid']
    client.cookies.clear()
    assert 'id="order-ecommerce"' not in client.get(url).text
