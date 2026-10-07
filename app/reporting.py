"""Order attribution and reports. Monetary totals remain integer minor units."""
import json
import re
import time
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Request
from . import db

router = APIRouter()
UTM = ('utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term')


def migrate(c):
    for table, fields in {
        'slides': {'layout': "TEXT NOT NULL DEFAULT 'split'", 'mobile_photo': "TEXT NOT NULL DEFAULT ''",
                   'text_align': "TEXT NOT NULL DEFAULT 'left'", 'text_color': "TEXT NOT NULL DEFAULT '#ffffff'",
                   'button_color': "TEXT NOT NULL DEFAULT '#ad3529'", 'button_text_color': "TEXT NOT NULL DEFAULT '#ffffff'",
                   'overlay': 'INTEGER NOT NULL DEFAULT 30', 'show_text': 'INTEGER NOT NULL DEFAULT 1',
                   'show_button': 'INTEGER NOT NULL DEFAULT 1'},
        'order_items': {'category_name': "TEXT NOT NULL DEFAULT ''"},
        'iiko_jobs': {'sync_next': 'REAL NOT NULL DEFAULT 0', 'sync_at': "TEXT NOT NULL DEFAULT ''",
                      'sync_status': "TEXT NOT NULL DEFAULT ''", 'sync_error': "TEXT NOT NULL DEFAULT ''"},
    }.items():
        existing = {r['name'] for r in c.execute(f'PRAGMA table_info({table})')}
        for name, definition in fields.items():
            if name not in existing:
                c.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
    c.execute('CREATE TABLE IF NOT EXISTS order_attribution(order_id INTEGER PRIMARY KEY REFERENCES orders(id), first_touch TEXT NOT NULL, last_touch TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS visitor_attribution(visitor INTEGER PRIMARY KEY, first_touch TEXT NOT NULL, last_touch TEXT NOT NULL, updated INTEGER NOT NULL)')
    c.execute('CREATE INDEX IF NOT EXISTS attribution_expiry ON visitor_attribution(updated)')
    if 'created_at' in {r['name'] for r in c.execute('PRAGMA table_info(orders)')}:
        c.execute('CREATE INDEX IF NOT EXISTS orders_created ON orders(created_at)')
    c.execute('CREATE INDEX IF NOT EXISTS iiko_sync_due ON iiko_jobs(state,sync_next)')
    c.executemany('INSERT OR IGNORE INTO settings VALUES (?,?)', [('metrika_enabled','0'),('metrika_id',''),('iiko_status_sync','1')])
    # Snapshot category on every new item, including native messenger orders.
    if 'category_id' in {r['name'] for r in c.execute('PRAGMA table_info(products)')}:
        c.execute('''CREATE TRIGGER IF NOT EXISTS item_category_snapshot AFTER INSERT ON order_items
        WHEN NEW.category_name='' BEGIN
        UPDATE order_items SET category_name=COALESCE((SELECT c.name FROM products p JOIN categories c ON c.id=p.category_id WHERE p.id=NEW.product_id),'') WHERE id=NEW.id;
            END''')


def capture(request):
    now = int(time.time())
    values = {k: re.sub(r'[\x00-\x1f\x7f]', '', request.query_params.get(k, '')).strip()[:100] for k in UTM}
    # Attribution stays server-side: long Cyrillic tags must not overflow cookies.
    with db.connect(True) as c:
        c.execute('DELETE FROM visitor_attribution WHERE updated<?',(now-90*86400,))
        if any(values.values()):
            payload = json.dumps(values,ensure_ascii=False)
            c.execute('''INSERT INTO visitor_attribution VALUES (?,?,?,?) ON CONFLICT(visitor)
                DO UPDATE SET last_touch=excluded.last_touch,updated=excluded.updated''',
                (request.session['visitor'],payload,payload,now))


def record(c, oid, request):
    touches = c.execute('SELECT * FROM visitor_attribution WHERE visitor=? AND updated>=?',(request.session.get('visitor'),time.time()-90*86400)).fetchone()
    if touches:
        c.execute('INSERT OR IGNORE INTO order_attribution VALUES (?,?,?)',
                  (oid, touches['first_touch'], touches['last_touch']))


def period(params):
    today = datetime.now(timezone(timedelta(hours=3))).date()
    try:
        end = date.fromisoformat(params.get('end') or today.isoformat())
        start = date.fromisoformat(params.get('start') or (end-timedelta(days=29)).isoformat())
    except ValueError:
        raise ValueError('Укажите даты в формате ГГГГ-ММ-ДД')
    if not 0 <= (end-start).days < 366:
        raise ValueError('Выберите период от 1 до 366 дней')
    def utc(day):
        return (datetime.combine(day, datetime.min.time())-timedelta(hours=3)).isoformat(' ')
    return start, end, utc(start), utc(end+timedelta(days=1))


def report(params):
    start,end,lo,hi = period(params)
    with db.connect() as c:
        currencies = [r[0] for r in c.execute('SELECT DISTINCT currency FROM orders ORDER BY currency')]
        currency = params.get('currency') or db.settings(c)['currency']
        channel = params.get('channel','')
        if channel not in ('','web','telegram','max','telegram_app','max_app'):
            channel = ''
        rows = [dict(r) for r in c.execute('''SELECT o.*,a.first_touch,a.last_touch,
            EXISTS(SELECT 1 FROM orders p WHERE p.phone_key=o.phone_key AND o.phone_key<>'' AND p.status<>'cancelled'
                AND (p.created_at<o.created_at OR (p.created_at=o.created_at AND p.id<o.id))) repeated
            FROM orders o LEFT JOIN order_attribution a ON a.order_id=o.id
            WHERE o.created_at>=? AND o.created_at<? AND o.currency=? AND (?='' OR o.channel=?)''',(lo,hi,currency,channel,channel))]
        prevlo = (datetime.fromisoformat(lo)-timedelta(days=(end-start).days+1)).isoformat(' ')
        previous = c.execute("SELECT count(*) n,COALESCE(sum(total),0) total FROM orders WHERE created_at>=? AND created_at<? AND currency=? AND status='done' AND (?='' OR channel=?)",(prevlo,lo,currency,channel,channel)).fetchone()
        products = c.execute("""SELECT i.name,sum(i.quantity) quantity,sum(i.price*i.quantity) amount FROM order_items i JOIN orders o ON o.id=i.order_id
            WHERE o.created_at>=? AND o.created_at<? AND o.currency=? AND o.status='done' AND (?='' OR o.channel=?)
            GROUP BY i.name ORDER BY quantity DESC LIMIT 15""",(lo,hi,currency,channel,channel)).fetchall()
        categories = c.execute("""SELECT COALESCE(NULLIF(i.category_name,''),'Без сохранённой категории') name,sum(i.quantity) quantity,sum(i.price*i.quantity) amount
            FROM order_items i JOIN orders o ON o.id=i.order_id WHERE o.created_at>=? AND o.created_at<? AND o.currency=? AND o.status='done' AND (?='' OR o.channel=?)
            GROUP BY i.category_name ORDER BY amount DESC""",(lo,hi,currency,channel,channel)).fetchall()
    done = [r for r in rows if r['status']=='done']
    total = sum(r['total'] for r in done)
    counts = {s: sum(r['status']==s for r in rows) for s in db.STATUSES}
    daily = []
    for offset in range((end-start).days+1):
        day = (start+timedelta(days=offset)).isoformat()
        orders = [r for r in done if (datetime.fromisoformat(r['created_at'])+timedelta(hours=3)).date().isoformat()==day]
        daily.append({'day':day, 'n':len(orders), 'amount':sum(r['total'] for r in orders)})
    peak = max([r['amount'] for r in daily]+[1])
    for d in daily: d['bar'] = round(d['amount']/peak*100)
    model = 'first_touch' if params.get('model')=='first' else 'last_touch'
    sources = {}
    for r in rows:
        touch = json.loads(r[model]) if r[model] else {}
        key = tuple(touch.get(k,'') for k in UTM)
        bucket = sources.setdefault(key, {'labels':key, 'n':0, 'done':0, 'amount':0})
        bucket['n'] += 1
        if r['status']=='done': bucket['done']+=1; bucket['amount']+=r['total']
    return dict(start=start,end=end,currency=currency,currencies=sorted(set(currencies+[currency])),channel=channel,
        model='first' if model=='first_touch' else 'last',counts=counts,n=len(rows),total=total,
        average=round(total/len(done)) if done else 0, previous=dict(previous),
        placed=sum(r['total'] for r in rows), paid=sum(r['total'] for r in rows if r['payment_method']=='tbank' and r['payment_status']=='paid'),
        refunds=sum(r['payment_status'] in ('refunded','partial_refund') for r in rows),
        repeated=sum(bool(r['repeated']) for r in rows if r['status']!='cancelled'),
        first=sum(not r['repeated'] for r in rows if r['status']!='cancelled'),
        pickup=sum(r['method']=='pickup' for r in rows),delivery=sum(r['method']=='delivery' for r in rows),
        channels=[(k,sum(r['channel']==k for r in rows)) for k in ('web','telegram','max','telegram_app','max_app')],
        daily=daily,products=products,categories=categories,sources=sorted(sources.values(),key=lambda x:-x['n']))


@router.get('/admin/analytics')
def analytics(request: Request):
    from .admin import require_admin, render
    require_admin(request)
    try:
        data = report(request.query_params)
        return render(request,'analytics.html',r=data,page='analytics')
    except ValueError as exc:
        return render(request,'analytics.html',r=None,page='analytics',error=str(exc))


@router.post('/admin/analytics/settings')
async def save_settings(request: Request):
    from .admin import form_data, field, redirect
    form = await form_data(request)
    try:
        counter = field(form,'metrika_id',15,False)
        enabled = form.get('metrika_enabled')=='on'
        if (counter and not re.fullmatch(r'[1-9][0-9]{0,14}',counter)) or (enabled and not counter):
            raise ValueError('Укажите числовой номер счётчика Метрики')
        with db.connect(True) as c:
            c.executemany('UPDATE settings SET value=? WHERE key=?',[(counter,'metrika_id'),('1' if enabled else '0','metrika_enabled'),('1' if form.get('iiko_status_sync')=='on' else '0','iiko_status_sync')])
        return redirect('/admin/analytics',ok='Настройки сохранены')
    except ValueError as exc:
        return redirect('/admin/analytics',error=exc)
    finally:
        await form.close()
