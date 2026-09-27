"""Persistent menu scheduling, independent of payment/order processing."""
import asyncio
import logging
import secrets
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Request

from . import db, menu_import as imp
from .vault import get_config, seal

router = APIRouter()
log = logging.getLogger(__name__)


async def sync_tick():
    now = time.time()
    owner = secrets.token_hex(16)
    with db.connect(True) as c:
        cfg = get_config('iiko', c)
        state = c.execute('SELECT * FROM menu_sync WHERE id=1').fetchone()
        # A negative deadline is an explicit one-off request, even with scheduling off.
        if state['lease_until'] > now or state['next_run'] > now:
            return
        if not cfg.get('auto_sync') and state['next_run'] >= 0 and not state['owner']:
            return
        interval = max(5, min(1440, int(cfg.get('sync_minutes', 5)))) * 60
        c.execute('UPDATE menu_sync SET owner=?,lease_until=?,next_run=? WHERE id=1',
                  (owner, now+180, now+interval))
    try:
        if not all(cfg.get(k) for k in ('api_key','app_id','client_secret','organization_id','external_menu')):
            raise ValueError('Сначала сохраните доступы iiko, организацию и внешнее меню.')
        body = {'externalMenuId':cfg['external_menu'], 'organizationIds':[cfg['organization_id']], 'version':2}
        if cfg.get('price_category'):
            body['priceCategoryId'] = cfg['price_category']
        menu = await asyncio.wait_for(imp.iiko_call(cfg, '/api/2/menu/by_id', body), timeout=90)
        data = imp.normalize_menu(menu, cfg)
        with db.connect(True) as c:
            current = c.execute('SELECT * FROM menu_sync WHERE id=1').fetchone()
            if current['owner'] != owner or current['lease_until'] <= time.time():
                return
            if get_config('iiko', c) != cfg:
                raise ValueError('Настройки изменились во время загрузки. Повторите обновление.')
            counts = imp.apply_rows(c, imp.prepare_preview(c, data))
            result = f"Создано: {counts['created']}; обновлено: {counts['updated']}; скрыто: {counts['hidden']}; фото в очереди: {counts['photos']}."
            if data['skipped']:
                result += f" Замечаний: {len(data['skipped'])}. Подробности доступны в предпросмотре."
            c.execute("UPDATE menu_sync SET lease_until=0,owner='',last_success=?,result=?,error='' WHERE id=1 AND owner=?",
                      (datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M UTC'),result,owner))
    except Exception as exc:
        error = str(exc) if type(exc) is ValueError else imp.menu_failure(exc,cfg,'menu_request')[1]
        with db.connect(True) as c:
            c.execute("UPDATE menu_sync SET lease_until=0,owner='',error=? WHERE id=1 AND owner=?",(error[:1500],owner))


async def worker():
    while True:
        try:
            await sync_tick()
        except Exception as exc:
            log.warning('Menu sync worker: %s',type(exc).__name__)
        await asyncio.sleep(3)


async def image_worker():
    while True:
        try:
            await imp.image_tick()
        except Exception as exc:
            log.warning('Menu photo worker: %s',type(exc).__name__)
        await asyncio.sleep(3)


@router.post('/admin/integrations/iiko/sync-settings')
async def sync_settings(request: Request):
    from .admin import form_data, redirect
    form = await form_data(request)
    try:
        minutes = int(form.get('sync_minutes',5))
        if not 5 <= minutes <= 1440:
            raise ValueError('Интервал должен быть от 5 до 1440 минут.')
        with db.connect(True) as c:
            cfg = get_config('iiko',c)
            cfg.update(auto_sync=form.get('auto_sync')=='on',sync_minutes=minutes)
            c.execute('INSERT INTO integrations VALUES (?,?) ON CONFLICT(name) DO UPDATE SET config=excluded.config',('iiko',seal(cfg)))
            c.execute("UPDATE menu_sync SET next_run=0 WHERE id=1")
        return redirect('/admin/integrations',ok='Расписание сохранено. Новые блюда создаются скрытыми; публикация — в разделе «Меню и товары».')
    except ValueError as exc:
        return redirect('/admin/integrations',error=exc)


@router.post('/admin/integrations/iiko/sync-now')
async def sync_now(request: Request):
    from .admin import form_data, redirect
    await form_data(request)
    with db.connect(True) as c:
        c.execute('UPDATE menu_sync SET next_run=-1 WHERE id=1')
    return redirect('/admin/integrations',ok='Обновление поставлено в очередь. Обновите страницу через минуту, чтобы увидеть результат.')


@router.post('/admin/integrations/iiko/photos-retry')
async def photos_retry(request: Request):
    from .admin import form_data, redirect
    await form_data(request)
    with db.connect(True) as c:
        for row in c.execute("SELECT product_id,url FROM menu_images WHERE state='failed'").fetchall():
            if imp.photo_allowed(row['url']):
                c.execute("UPDATE menu_images SET state='pending',attempts=0,error='',job_token=? WHERE product_id=?",(secrets.token_hex(16),row['product_id']))
    return redirect('/admin/integrations',ok='Доступные фотографии поставлены в очередь повторной загрузки.')


@router.post('/admin/products/bulk')
async def bulk_products(request: Request):
    from .admin import form_data, redirect
    # One field per selected dish, plus CSRF and the clicked action button.
    form = await form_data(request, max_fields=1002)
    try:
        action = str(form.get('action',''))
        if action not in ('publish','hide','publish_all'):
            raise ValueError('Выберите действие.')
        ids = {int(v) for v in form.getlist('product_id')}
        if action!='publish_all' and not ids:
            raise ValueError('Сначала отметьте блюда.')
        with db.connect(True) as c:
            rows = c.execute('SELECT id,active,iiko_available FROM products').fetchall()
            changed = blocked = 0
            for row in rows:
                if action!='publish_all' and row['id'] not in ids:
                    continue
                if action!='hide' and not row['iiko_available']:
                    blocked += 1
                    continue
                active = int(action!='hide')
                c.execute('UPDATE products SET active=?,iiko_resume_active=0 WHERE id=?',(active,row['id']))
                changed += int(row['active']!=active)
        return redirect('/admin/products',ok=f'Изменена публикация блюд: {changed}. Недоступны в iiko: {blocked}.')
    except ValueError as exc:
        return redirect('/admin/products',error=exc)
    finally:
        await form.close()
