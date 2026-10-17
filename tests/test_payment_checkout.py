import asyncio
import time
from urllib.parse import urlsplit

import httpx

from app import db, integrations as bank, customer_accounts as accounts
from app.vault import set_config
from test_admin import login
from test_customer_accounts import link
from test_integrations import configure_bank
from test_store import session, draft


def checkout(client):
    configure_bank()
    headers=session(client)
    data=draft(client,headers,'tbank')
    result=client.post('/api/checkout',headers=headers,json=data)
    assert result.status_code==200
    assert result.json()['url'].endswith('?pay=1')
    path=urlsplit(result.json()['url']).path
    return headers,data,path


def fake_init(monkeypatch, calls):
    async def call(cfg,method,data):
        calls.append(method)
        assert method=='Init'
        return {'Success':True,'Amount':data['Amount'],'OrderId':data['OrderId'],
                'PaymentId':'p1','PaymentURL':'https://securepay.tinkoff.ru/payment-test'}
    monkeypatch.setattr(bank,'bank_call',call)


def confirm(oid):
    row=bank.payment_row(oid)
    bank.apply_payment(row,{'TerminalKey':'test-terminal','PaymentId':'p1','Amount':row['total'],
                           'OrderId':row['bank_order'],'Status':'CONFIRMED'})


def test_checkout_stays_out_of_orders_until_bank_confirmation(client,monkeypatch):
    link()
    with db.connect(True) as c:c.execute("UPDATE settings SET value='1' WHERE key='auto_accept'")
    set_config('iiko',{'enabled':True})
    headers,data,path=checkout(client)
    # Double submit resumes the same payment; no second checkout or charge.
    assert client.post('/api/checkout',headers=headers,json=data).json()['url']==path+'?pay=1'
    page=client.get(path+'?pay=1').text
    assert '<h1>Ожидаем оплату</h1>' in page and 'Спасибо! Мы получили ваш заказ.' not in page
    assert 'Ваши заказы — в боте' not in page and 'data-auto-pay="1"' in page
    assert accounts.history('telegram',123456,321)[0]==[]
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM orders').fetchone()[0]==1
        for table in ('order_intake','outbox','customer_messages','iiko_jobs'):
            assert c.execute(f'SELECT count(*) FROM {table}').fetchone()[0]==0
    calls=[];fake_init(monkeypatch,calls)
    assert client.post('/api'+path+'/payment',headers=headers,json={}).status_code==200
    assert client.post('/api'+path+'/payment',headers=headers,json={}).status_code==200
    assert calls==['Init']
    assert client.get('/api'+path+'/status').json()['payment_ready']
    login(client)
    assert client.get('/admin/order-feed?inbox=true').json()['recent']==[]
    assert 'data-order="1"' not in client.get('/admin/orders').text
    from app.reporting import report
    assert report({})['n']==0
    confirm(1);confirm(1)
    with db.connect() as c:
        assert c.execute('SELECT status FROM orders').fetchone()[0]=='accepted'
        assert c.execute('SELECT count(*) FROM order_intake').fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM iiko_jobs').fetchone()[0]==1
        assert c.execute("SELECT count(*) FROM customer_messages WHERE event LIKE 'created:%'").fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM outbox').fetchone()[0]==1
    assert report({})['n']==1
    assert len(accounts.history('telegram',123456,321)[0])==1
    assert client.get('/admin/order-feed?after=0').json()['orders']==[1]
    assert 'Оплата подтверждена банком.' in client.get(path).text
    assert 'data-payment-link' not in client.get(path+'?pay=1').text


def test_payment_start_requires_owner_and_csrf_and_handles_bank_failure(client,monkeypatch):
    headers,data,path=checkout(client)
    calls=[]
    async def fail(cfg,method,data):
        calls.append(method)
        raise httpx.ReadTimeout('SECRET provider text')
    monkeypatch.setattr(bank,'bank_call',fail)
    assert client.post('/api'+path+'/payment',json={}).status_code==403
    assert not calls
    assert client.post('/api'+path+'/payment',headers=headers,json={}).status_code==200
    assert client.post('/api'+path+'/payment',headers=headers,json={}).status_code==200
    assert calls==['Init']  # Ambiguous Init is reconciled by the worker, never repeated here.
    state=client.get('/api'+path+'/status').json()
    assert state['payment_error'] and not state['payment_ready'] and state['payment_status']=='pending'
    page=client.get(path).text
    assert 'Не удалось получить ссылку на оплату' in page and 'SECRET' not in page
    client.cookies.clear();other=session(client)
    assert client.post('/api'+path+'/payment',headers=other,json={}).status_code==404
    assert calls==['Init']
    # Reconciliation can recover a payment ID without recovering its checkout URL.
    with db.connect(True) as c:
        c.execute("UPDATE payments SET payment_id='p1',state='NEW',error='' WHERE order_id=1")
    assert client.get('/api'+path+'/status').json()['payment_error']
    assert 'Не удалось получить ссылку на оплату' in client.get(path).text


def test_older_paid_checkout_is_announced_after_newer_cash_order(client,monkeypatch):
    headers,data,path=checkout(client)
    calls=[];fake_init(monkeypatch,calls)
    client.post('/api'+path+'/payment',headers=headers,json={})
    headers=session(client)
    assert client.post('/api/checkout',headers=headers,json=draft(client,headers)).status_code==200
    login(client)
    first=client.get('/admin/order-feed?inbox=true&after=0').json()
    assert first['orders']==[2] and first['cursor']==1
    confirm(1)
    second=client.get('/admin/order-feed?inbox=true&after=1').json()
    assert second['orders']==[1] and second['cursor']==2
    assert [(r['id'],r['event_id']) for r in second['recent']]==[(1,2),(2,1)]
    assert client.get('/admin/order-feed?after=2').json()['orders']==[]
    db.init();db.init()
    assert client.get('/admin/order-feed?after=2').json()['orders']==[]


def test_failed_expired_and_cancelled_payments_never_show_active_payment_link(client,monkeypatch):
    headers,data,path=checkout(client)
    calls=[];fake_init(monkeypatch,calls)
    client.post('/api'+path+'/payment',headers=headers,json={})
    with db.connect(True) as c:c.execute('UPDATE payments SET expires=?',(time.time()-1,))
    assert client.get('/api'+path+'/status').json()['payment_expired']
    assert 'data-payment-link' not in client.get(path+'?pay=1').text
    with db.connect(True) as c:
        c.execute('UPDATE payments SET expires=?',(time.time()+1000,))
        c.execute("UPDATE orders SET payment_status='failed'")
    assert not client.get('/api'+path+'/status').json()['payment_ready']
    assert 'data-payment-link' not in client.get(path+'?pay=1').text
    with db.connect(True) as c:c.execute("UPDATE orders SET payment_status='pending',status='cancelled'")
    assert not client.get('/api'+path+'/status').json()['payment_ready']
    assert 'data-payment-link' not in client.get(path+'?pay=1').text
