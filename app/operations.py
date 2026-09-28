"""Explicit VPS operations; never print credentials or raw provider error bodies."""
import asyncio
import json

from . import db, config, menu_import as imp
from .integrations import iiko_call
from .vault import get_config, seal


def telegram_admin_add(uid):
    if type(uid) is not int or not 0 < uid < 2**63:
        raise ValueError('Нужен положительный числовой Telegram ID сотрудника.')
    with db.connect(True) as c:
        cfg = get_config('telegram', c)
        ids = list(dict.fromkeys([*cfg.get('admin_ids', config.ADMIN_IDS), uid]))
        cfg['admin_ids'] = ids
        c.execute('INSERT INTO integrations VALUES (?,?) ON CONFLICT(name) DO UPDATE SET config=excluded.config', ('telegram', seal(cfg)))
    print('Получатели Telegram:', ', '.join(map(str, ids)))
    print('Сотрудник должен отправить /start боту магазина. Это получатель уведомлений, а не учётная запись веб-админки.')


async def iiko_menus(menu_id=None, price_category=None, apply=False):
    cfg = get_config('iiko')
    listing = await asyncio.wait_for(iiko_call(cfg, '/api/2/menu', {}), 45)
    menus = listing.get('externalMenus') or []
    if menu_id is None:
        print(json.dumps({'externalMenus':[{k:m.get(k) for k in ('id','name')} for m in menus],
            'priceCategories':[{k:p.get(k) for k in ('id','name')} for p in listing.get('priceCategories') or []]}, ensure_ascii=False, indent=2))
        return
    if str(menu_id) not in {str(m.get('id')) for m in menus}:
        raise ValueError('Такое меню отсутствует в списке доступных этому API-ключу. Проверьте привязку в Cloud API iikoWeb.')
    target = dict(cfg, external_menu=str(menu_id))
    if price_category is not None:
        from .integrations import uuid_field
        target['price_category'] = uuid_field(price_category)
    body = {'externalMenuId':target['external_menu'], 'organizationIds':[target['organization_id']], 'version':2}
    if target.get('price_category'):body['priceCategoryId'] = target['price_category']
    menu = await asyncio.wait_for(iiko_call(target, '/api/2/menu/by_id', body), 90)
    data = imp.normalize_menu(menu, target)
    with db.connect() as c:
        fingerprint = imp.catalog_fingerprint(c)
        imp.prepare_preview(c, data, replace_source=imp.source_key(cfg))
    print('Меню:', data['name'], '| ID:', target['external_menu'])
    print('Создать:', sum(r['local_id'] is None for r in data['rows']), '| Обновить:', sum(r['local_id'] is not None for r in data['rows']), '| Скрыть:', len(data['hide_ids']))
    for row in data['rows'][:10]:print(row['category'], '·', row['name'], '·', db.money(row['price']))
    print('Замечаний:', len(data['skipped']))
    if not apply:
        print('Это предпросмотр. Для переключения повторите команду с --apply. Новые блюда появятся скрытыми.')
        return
    with db.connect(True) as c:
        if get_config('iiko', c) != cfg or imp.catalog_fingerprint(c) != fingerprint:
            raise ValueError('Настройки или каталог изменились. Повторите предпросмотр.')
        counts = imp.apply_rows(c, data)
        c.execute('UPDATE integrations SET config=? WHERE name=?', (seal(target), 'iiko'))
        c.execute("UPDATE menu_sync SET owner='',lease_until=0,next_run=0,error='' WHERE id=1")
    print('Меню переключено:', json.dumps(counts, ensure_ascii=False))
    print('Общие карточки сохранены; отсутствующие позиции скрыты. Проверьте и опубликуйте новые блюда в админке.')
