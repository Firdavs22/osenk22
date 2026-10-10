from app import db, reporting
from test_admin import login
from test_analytics_banners_sync import place


def test_product_filters_use_snapshots_and_do_not_change_totals(client):
    for _ in range(3): place(client)
    with db.connect(True) as c:
        c.execute("UPDATE orders SET status='done',created_at='2026-10-10 10:00:00'")
        c.execute("UPDATE order_items SET name='Ролл с лососем',category_name='Роллы',price=10000,quantity=3 WHERE order_id=1")
        c.execute("UPDATE order_items SET name='СЕТ',category_name='Сеты',price=50000,quantity=1 WHERE order_id=2")
        c.execute("UPDATE order_items SET name='Суп',category_name='',price=20000,quantity=2 WHERE order_id=3")
    dates={'start':'2026-10-10','end':'2026-10-10'}
    r=reporting.report(dates)
    assert [p['name'] for p in r['products']]==['Ролл с лососем','Суп','СЕТ']
    by_amount=reporting.report(dates|{'product_sort':'amount'})
    assert [p['name'] for p in by_amount['products']]==['СЕТ','Суп','Ролл с лососем']
    filtered=reporting.report(dates|{'product_q':'ролл','product_category':'Роллы'})
    assert filtered['product_count']==1 and filtered['products'][0]['quantity']==3
    assert filtered['total']==r['total'] and filtered['n']==3
    assert reporting.report(dates|{'product_category':'Сеты','product_q':'суп'})['products']==[]
    assert reporting.report(dates|{'product_sort':'bad','product_limit':'-1'})['product_limit']=='15'
    login(client)
    text=client.get('/admin/analytics',params=dates|{'product_q':'<script>'}).text
    assert '&lt;script&gt;' in text and '<script>' not in text


def test_archive_date_boundaries_pagination_and_modal(client):
    for _ in range(3): place(client)
    with db.connect(True) as c:
        c.execute("UPDATE orders SET status='done',created_at='2026-10-09 21:00:00' WHERE id=1")
        c.execute("UPDATE orders SET status='cancelled',created_at='2026-10-10 20:59:59' WHERE id=2")
        c.execute("UPDATE orders SET status='done',created_at='2026-10-10 21:00:00' WHERE id=3")
    login(client)
    query={'start':'2026-10-10','end':'2026-10-10','fragment':'true'}
    text=client.get('/admin/orders',params=query).text
    assert 'data-order="1"' in text and 'data-order="2"' in text and 'data-order="3"' not in text
    assert 'archive-card' in text and 'card-address' not in text and 'card-counts' not in text
    assert 'data-order-detail' in client.get('/admin/orders/1?fragment=true').text
    assert 'data-order="2"' not in client.get('/admin/orders',params=query|{'status':'done'}).text
    invalid=client.get('/admin/orders?start=bad').text
    assert 'Укажите даты' in invalid and 'data-order="1"' not in invalid
    page=client.get('/admin/orders',params=query|{'page_num':2}).text
    assert 'start=2026-10-10&end=2026-10-10&page_num=1' in page


def test_counter_settings_moved_and_old_post_stays_compatible(client):
    assert client.get('/admin/settings/analytics',follow_redirects=False).status_code==303
    csrf=login(client)
    assert 'name="metrika_id"' not in client.get('/admin/analytics').text
    assert 'name="metrika_id"' in client.get('/admin/settings/analytics').text
    assert 'href="/admin/integrations"' in client.get('/admin/settings').text
    assert client.post('/admin/settings/analytics',data={'metrika_id':'1'}).status_code==403
    response=client.post('/admin/settings/analytics',data={'csrf':csrf,'metrika_id':'123','metrika_enabled':'on'},follow_redirects=False)
    assert response.headers['location'].startswith('/admin/settings/analytics?ok=')
    assert db.settings()['metrika_id']=='123'
