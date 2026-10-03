import asyncio
import copy
import uuid

from aiogram import Bot
import re

from app import db, customer_accounts as accounts, menu_import as imp, max_bot
from app.vault import set_config
from test_admin import login
from test_bot import FakeTelegram
from test_customer_accounts import link, message
from test_order_updates import order, setup_bot, TOKEN
from test_menu_import import CFG, menu, batch
from test_messengers import MAX


def test_returning_customer_keeps_history_and_gets_second_order_automatically(client):
    setup_bot(); link(); order(client); order(client)
    async def run():
        from app.bot import commands, show_menu
        fake = FakeTelegram(); bot = Bot(TOKEN,session=fake)
        await commands(message(bot,'/start account'))
        assert not any(getattr(m.reply_markup,'keyboard',None) for m in fake.calls if hasattr(m,'reply_markup'))
        texts = [m.text for m in fake.calls if getattr(m,'text',None)]
        assert any('Заказ №1' in t for t in texts) and any('Заказ №2' in t for t in texts)
        await show_menu(message(bot,'/menu'))
        assert any(b.callback_data=='orders' for row in fake.calls[-1].reply_markup.inline_keyboard for b in row)
    asyncio.run(run())
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM customer_accounts').fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM customer_orders').fetchone()[0]==2
        assert c.execute("SELECT count(*) FROM customer_messages WHERE event LIKE 'created:%'").fetchone()[0]==2
        assert not c.execute('SELECT 1 FROM customer_contact_requests').fetchone()
    db.set_status(2,'accepted')
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM customer_messages WHERE order_id=2 AND event LIKE 'status:%'").fetchone()[0]==1


def test_resume_preserves_opt_out_and_does_not_link_another_user(shop):
    link(); accounts.stop('telegram',123456,321)
    assert accounts.resume('telegram',123456,321,321)['notifications']==0
    assert accounts.resume('telegram',123456,322,322) is None
    assert accounts.resume('telegram',999,321,321) is None
    assert accounts.resume('telegram',123456,321,321,enable=True)['notifications']==1


def test_returning_max_account_shows_history_without_contact_request(client,monkeypatch):
    set_config('max',MAX); order(client)
    key=max_bot.bot_key(MAX); link('max',key,555,123)
    calls=[]
    async def api(cfg,method,path,body=None,params=None):
        calls.append(body)
        return {'message':{'body':{'mid':'result'}}}
    monkeypatch.setattr(max_bot,'api',api)
    asyncio.run(max_bot.process_event(MAX,{'action':'account','user_id':555,'chat_id':123,'callback_id':''}))
    assert 'Заказ №1' in calls[-1]['text']
    assert 'С возвращением' in calls[-1]['text']
    assert 'request_contact' not in str(calls)


def test_order_feed_private_bounded_and_no_customer_data(client,monkeypatch):
    assert client.get('/admin/order-feed',follow_redirects=False).status_code==303
    order(client); order(client); login(client)
    initial=client.get('/admin/order-feed').json()
    assert initial=={'cursor':2,'orders':[],'pending':2}
    assert client.get('/admin/order-feed?after=1').json()['orders']==[2]
    assert client.get('/admin/order-feed?after=2').json()['orders']==[]
    assert client.get('/admin/order-feed?after=garbage').status_code==422
    monkeypatch.setattr('app.store.limit',lambda *args:None)
    for _ in range(51): order(client)
    page=client.get('/admin/order-feed?after=0').json()
    assert len(page['orders'])==50 and page['cursor']==50
    assert client.get('/admin/order-feed?after=50').json()['orders']==[51,52,53]


def test_quick_progress_counts_and_duplicate_submission(client):
    order(client); order(client); csrf=login(client)
    page=client.get('/admin/orders').text
    assert 'Заказов у покупателя: 2' in page and 'Блюд:' in page
    assert 'data-order="2"' in page and 'class="quick-status"' in page
    for current,nxt in [('new','accepted'),('accepted','cooking'),('cooking','ready'),('ready','done')]:
        doc=client.get('/admin/orders?status='+current).text
        form=re.search(r'<form[^>]+action="/admin/orders/2/status"[^>]*>(.*?)</form>',doc,re.S).group(1)
        assert f'name="status" value="{nxt}"' in form
        payload={'csrf':csrf,'status':nxt}
        assert client.post('/admin/orders/2/status',data=payload,headers={'Accept':'application/json'}).status_code==200
        assert client.post('/admin/orders/2/status',data=payload,headers={'Accept':'application/json'}).status_code==400
    done=client.get('/admin/orders?status=done').text
    assert 'class="quick-status"' not in done


def test_iiko_categories_moves_hidden_groups_and_repeat_import(shop):
    set_config('iiko',CFG)
    data=menu(); first=data['itemCategories'][0]; first['name']='  Роллы  '
    second=copy.deepcopy(first); second['id']='cat2';second['name']='Запечённые роллы'
    second['items'][0]['itemId']=str(uuid.uuid4()); second['isHidden']=True
    data['itemCategories'].append(second)
    imp.apply_import(batch(data))
    rows=[p for p in db.products() if p['iiko_id']]
    categories={c['id']:c['name'] for c in db.categories()}
    assert {categories[p['category_id']] for p in rows}=={'Роллы','Запечённые роллы'}
    assert not next(p for p in rows if p['iiko_id']==second['items'][0]['itemId'])['iiko_available']
    first['name']='РОЛЛЫ';imp.apply_import(batch(data))
    assert len([p for p in db.products() if p['iiko_id']])==2
    assert len([c for c in db.categories() if c['name'].lower()=='роллы'])==1
    moved=first['items'].pop();second['items'].append(moved)
    imp.apply_import(batch(data))
    p=next(p for p in db.products() if p['iiko_id']==moved['itemId'])
    assert categories[p['category_id']]=='Запечённые роллы'
    diagnostic=imp.diagnostic(data,CFG)
    assert diagnostic['categories'][1]['items']==2


def test_tags_deduplicated_without_merging_products(client):
    with db.connect(True) as c:
        c.execute("UPDATE products SET tags=' Хит , хит, ХИТ,  С   лососем '")
    doc=client.get('/').text
    assert re.findall(r'<button[^>]+data-tag="[^"]*"[^>]*>([^<]+)</button>',doc)==['Все вкусы','Хит','С лососем']
    assert len(re.findall(r'data-product="',doc))==5
    assert re.findall(r'data-tags="([^"]*)"',doc)==['Хит, С лососем']*5
    assert 'Готовим после подтверждения' not in doc
