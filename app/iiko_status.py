"""Read only reconciliation of deliveries already created by this shop."""
import json
import time
from . import db
from .vault import unseal

STATES = {'Unconfirmed':'new', 'WaitCooking':'accepted', 'ReadyForCooking':'accepted',
          'CookingStarted':'cooking', 'CookingCompleted':'ready', 'Waiting':'ready',
          'OnWay':'ready', 'Delivered':'done', 'Closed':'done', 'Cancelled':'cancelled'}


def apply(c, job, result):
    found = next((o for o in result.get('orders', []) if o.get('id')==job['external_id']), None)
    if not found or found.get('creationStatus')!='Success' or not isinstance(found.get('order'),dict):
        raise ValueError('iiko пока не вернула состояние заказа')
    remote = found['order'].get('status','')
    if remote not in STATES:
        raise ValueError('iiko вернула неизвестный статус заказа')
    db.set_status(job['order_id'], STATES[remote], c, from_iiko=True)
    c.execute("UPDATE iiko_jobs SET sync_status=?,sync_at=strftime('%Y-%m-%d %H:%M:%S','now'),sync_error='' WHERE order_id=?",(remote,job['order_id']))


async def tick():
    from .integrations import iiko_call
    now = time.time()
    with db.connect(True) as c:
        if db.settings(c).get('iiko_status_sync')!='1': return
        jobs = [dict(r) for r in c.execute("""SELECT j.* FROM iiko_jobs j JOIN orders o ON o.id=j.order_id
            WHERE j.state='sent' AND j.sync_next<=? AND o.status NOT IN ('done','cancelled')
            ORDER BY j.sync_next,j.order_id LIMIT 20""",(now,))]
        # Claim before network calls; retries never resend /create.
        c.executemany('UPDATE iiko_jobs SET sync_next=? WHERE order_id=?',[(now+60,r['order_id']) for r in jobs])
    groups = {}
    for job in jobs:
        try:
            cfg = unseal(job['credentials'])
            groups.setdefault(json.dumps(cfg,sort_keys=True), (cfg,[]))[1].append(job)
        except Exception:
            with db.connect(True) as c:
                c.execute("UPDATE iiko_jobs SET sync_error='Не удалось прочитать настройки заказа',sync_next=? WHERE order_id=?",(now+300,job['order_id']))
    for cfg, group in groups.values():
        try:
            result = await iiko_call(cfg,'/api/1/deliveries/by_id',{'organizationId':cfg['organization_id'],'orderIds':[r['external_id'] for r in group]})
        except Exception:
            with db.connect(True) as c:
                c.executemany("UPDATE iiko_jobs SET sync_error='Нет ответа iiko. Повторим проверку.',sync_next=? WHERE order_id=?",[(now+120,r['order_id']) for r in group])
            continue
        for job in group:
            try:
                with db.connect(True) as c: apply(c,job,result)
            except (ValueError,TypeError,AttributeError):
                with db.connect(True) as c:
                    c.execute("UPDATE iiko_jobs SET sync_error='Не удалось обновить статус. Проверьте состояние и оплату заказа в iiko.',sync_next=? WHERE order_id=?",(now+120,job['order_id']))
