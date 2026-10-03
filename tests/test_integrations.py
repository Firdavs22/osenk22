import asyncio
import uuid

import httpx
import pytest

from app import db, integrations as api
from app.vault import set_config
from test_store import session, draft


def configure_bank():
    set_config('tbank',{'enabled':True,'terminal':'test-terminal','password':'test-secret',
        'public_url':'https://shop.example.com','fiscal_ready':True,'tax':'none','delivery_tax':'none','taxation':'usn_income'})


def create_online(client):
    configure_bank(); headers=session(client)
    response=client.post('/api/checkout',headers=headers,json=draft(client,headers,'tbank'))
    assert response.status_code==200,response.text
    with db.connect() as c:
        return c.execute('SELECT id FROM orders ORDER BY id DESC').fetchone()[0]


def test_bank_signature_official_vector():
    # Official request-signature example (steps 1–5, including Cyrillic).
    fields={'TerminalKey':'MerchantTerminalKey','Amount':19200,'OrderId':'00000',
            'Description':'Подарочная карта на 1000 рублей','Receipt':{},'DATA':{'ignored':'yes'}}
    assert api.bank_token(fields,'11111111111111')=='72dd466f8ace0a37a1f740ce5fb78101712bc0665d91a8108c7c8a0ccd426db2'


def test_payment_verified_amount_idempotency_and_refund(client,monkeypatch):
    oid=create_online(client);calls=[];state='AUTHORIZED';amount=59000
    async def bank(cfg,method,data):
        calls.append((method,data))
        row=api.payment_row(oid)
        if method=='Init':
            assert data['Amount']==59000
            assert sum(x['Amount'] for x in data['Receipt']['Items'])==59000
            assert data['PayType']=='O'
            return {'Success':True,'Amount':59000,'OrderId':row['bank_order'],'PaymentId':'p1','PaymentURL':'https://securepay.tinkoff.ru/test'}
        return {'Success':True,'TerminalKey':'test-terminal','PaymentId':'p1','Amount':amount,'Status':state,'OrderId':row['bank_order']}
    monkeypatch.setattr(api,'bank_call',bank)
    asyncio.run(api.process_payment(oid));asyncio.run(api.process_payment(oid))
    with pytest.raises(ValueError):db.set_status(oid,'accepted')
    state='CONFIRMED';amount=1
    with pytest.raises(ValueError):asyncio.run(api.process_payment(oid))
    amount=59000
    asyncio.run(api.process_payment(oid));asyncio.run(api.process_payment(oid))
    with db.connect() as c:
        assert c.execute('SELECT payment_status FROM orders').fetchone()[0]=='paid'
        assert c.execute('SELECT count(*) FROM outbox').fetchone()[0]==1
    db.set_status(oid,'accepted')
    state='REFUNDED';asyncio.run(api.process_payment(oid))
    state='CONFIRMED';asyncio.run(api.process_payment(oid))
    assert api.payment_row(oid)['payment_status']=='refunded'
    assert sum(method=='Init' for method,_ in calls)==1


def test_ambiguous_init_recovers_without_creating_second_payment(client,monkeypatch):
    oid=create_online(client);calls=[]
    async def bank(cfg,method,data):
        calls.append(method)
        if method=='Init':raise httpx.ReadTimeout('timeout')
        if method=='CheckOrder':return {'Payments':[{'PaymentId':'p1'}]}
        row=api.payment_row(oid)
        return {'TerminalKey':'test-terminal','PaymentId':'p1','Amount':59000,'OrderId':row['bank_order'],'Status':'CONFIRMED'}
    monkeypatch.setattr(api,'bank_call',bank)
    with pytest.raises(httpx.ReadTimeout):asyncio.run(api.process_payment(oid))
    asyncio.run(api.process_payment(oid))
    assert calls==['Init','CheckOrder','GetState']
    assert api.payment_row(oid)['payment_status']=='paid'


def test_webhook_forgery_and_signed_callback_cannot_mark_paid(client):
    oid=create_online(client);row=api.payment_row(oid)
    data={'TerminalKey':'test-terminal','OrderId':row['bank_order'],'PaymentId':'p1','Amount':59000,'Status':'CONFIRMED','Success':True,'Token':'bad'}
    assert client.post('/api/payments/tbank/webhook',json=data).status_code==400
    data['Token']=api.bank_token(data,'test-secret')
    response=client.post('/api/payments/tbank/webhook',json=data)
    assert response.text=='OK'
    assert api.payment_row(oid)['payment_status']=='pending'


def test_iiko_uuid_payload_and_ambiguous_create_no_duplicate(client,monkeypatch):
    u=lambda:str(uuid.uuid4())
    cfg={'enabled':True,'api_key':'key','app_id':u(),'client_secret':'secret','organization_id':u(),'terminal_group':u(),'city_format':True}
    set_config('iiko',cfg)
    with db.connect(True) as c:c.execute('UPDATE products SET iiko_id=? WHERE id=1',(u(),))
    headers=session(client)
    assert client.post('/api/checkout',headers=headers,json=draft(client,headers)).status_code==200
    db.set_status(1,'accepted')
    calls=[]
    async def iiko(config,path,body,token=None):
        calls.append((path,body))
        if path.endswith('/create'):
            assert body['order']['orderServiceType']=='DeliveryByClient'
            assert 'payments' not in body['order']  # cash is still unpaid
            assert body['createOrderSettings']['checkStopList'] is True
            raise httpx.ReadTimeout('unknown outcome')
        return {'orders':[{'id':body['orderIds'][0],'creationStatus':'Success'}]}
    monkeypatch.setattr(api,'iiko_call',iiko)
    with pytest.raises(httpx.ReadTimeout):asyncio.run(api.process_iiko(1))
    asyncio.run(api.process_iiko(1))
    assert [p for p,_ in calls]==['/api/1/deliveries/create','/api/1/deliveries/by_id']
    with db.connect() as c:assert c.execute('SELECT state FROM iiko_jobs').fetchone()[0]=='sent'
