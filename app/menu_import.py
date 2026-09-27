"""Reviewable import of the explicit iiko external-menu v2 contract."""
import asyncio
import hashlib
import io
import json
import re
import secrets
import time
from collections import Counter
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile

from . import config, db
from .integrations import iiko_call, uuid_field
from .vault import get_config

router = APIRouter()


def source_key(cfg):
    return '|'.join(str(cfg.get(k) or '') for k in ('organization_id', 'external_menu', 'price_category'))


def catalog_fingerprint(c):
    snapshot = [[dict(r) for r in c.execute('SELECT * FROM products ORDER BY id')],
                [dict(r) for r in c.execute('SELECT * FROM categories ORDER BY id')]]
    return hashlib.sha256(json.dumps(snapshot,sort_keys=True).encode()).hexdigest()


def photo_allowed(url):
    try:
        p = urlsplit(url)
        # iiko documents this managed CDN for external-menu images. Never fetch arbitrary
        # hosts, IP addresses, ports, credentials or redirects supplied by a menu.
        return p.scheme == 'https' and bool(re.fullmatch(r'\d+\.selcdn\.ru', p.hostname or '')) and p.port in (None,443) and not p.username and not p.password
    except ValueError:
        return False


def optional_modifiers(groups):
    """Only omit extras explicitly permitting zero, with no default selections."""
    def optional(restriction):
        return (isinstance(restriction, dict)
                and restriction.get('minQuantity') == 0
                and restriction.get('byDefault', 0) == 0)
    for group in groups:
        if not optional(group.get('restrictions')):
            return False
        for item in group.get('items') or []:
            restrictions = item.get('restrictions')
            if isinstance(restrictions, dict):
                restrictions = [restrictions]
            if group.get('childModifiersHaveMinMaxRestrictions') and not restrictions:
                return False
            if any(not optional(r) for r in restrictions or []):
                return False
    return True


def normalize_menu(menu, cfg, allow_empty=False):
    if menu.get('formatVersion') != 2 or not isinstance(menu.get('itemCategories'),list):
        raise ValueError('iiko вернула неподдерживаемый формат меню. Нужен внешний каталог версии 2.')
    if str(menu.get('id')) != str(cfg['external_menu']):
        raise ValueError('ID полученного меню не совпал с выбранным')
    if menu.get('intervals'):
        raise ValueError('Меню с расписанием требует поддержки расписаний. Выберите меню без временных ограничений.')
    rows, skipped, seen = [], [], set()
    for category in menu['itemCategories']:
        category_name = str(category.get('name') or '').strip()
        if not category_name or len(category_name)>50:
            raise ValueError('Название категории должно содержать от 1 до 50 символов')
        for item in category.get('items') or []:
            title = str(item.get('name') or '').strip()
            try:
                product_id = uuid_field(item.get('itemId') or '')
                if not product_id:
                    raise ValueError('Нет UUID блюда')
                if item.get('type','DISH') != 'DISH' or item.get('orderItemType','Product') != 'Product':
                    skipped.append(title + ': комбо или составное блюдо'); continue
                if item.get('canSetOpenPrice') or item.get('isMarked'):
                    skipped.append(title + ': открытая цена или маркировка не поддерживаются'); continue
                if category.get('schedules') or category.get('scheduleId'):
                    skipped.append(title + ': категория с расписанием'); continue
                sizes = item.get('itemSizes') or []
                if not sizes:
                    raise ValueError('Нет размеров/цен')
                for size in sizes:
                    size_id = uuid_field(size.get('sizeId') or '')
                    identity = (product_id,size_id)
                    groups = size.get('itemModifierGroups') or []
                    if groups and not optional_modifiers(groups):
                        skipped.append(title + ': обязательные модификаторы, выбранные по умолчанию добавки или неизвестные ограничения')
                        continue
                    if groups:
                        skipped.append(title + ': импортируется базовое блюдо без необязательных добавок')
                    prices = [p.get('price') for p in size.get('prices') or [] if cfg['organization_id'].lower() in [str(o).lower() for o in p.get('organizations') or []]]
                    if len(prices)!=1 or prices[0] is None:
                        skipped.append(title + ': нет однозначной цены выбранной организации')
                        continue
                    price = db.parse_money(prices[0])
                    if price == 0:
                        skipped.append(title + ': нулевая цена; карточка не импортируется')
                        continue
                    name = title + (' · '+str(size.get('sizeName')) if size.get('sizeName') else '')
                    if not title or len(name)>80:
                        raise ValueError('Название блюда с размером должно содержать до 80 символов')
                    weight = Decimal(str(size.get('portionWeightGrams') or 0))
                    if not weight.is_finite() or weight<0 or weight>100000:
                        raise ValueError('Некорректный вес')
                    description = str(item.get('description') or '').strip()
                    allergens = ', '.join(str(a.get('name') or '') for a in item.get('allergens') or [] if a.get('name') and not a.get('isDeleted'))
                    if allergens:
                        description += '\nАллергены по данным iiko: '+allergens
                    labels = ', '.join(dict.fromkeys(str(t['name']) for t in item.get('labels') or [] if t.get('name')))
                    if len(description)>1000 or len(labels)>160:
                        raise ValueError('Описание или теги слишком длинные для карточки')
                    record = {'iiko_id':product_id,'iiko_size':size_id,'name':name,'category':category_name,
                              'price':price,'description':description,'weight':f'{weight.normalize():f} г' if weight else '',
                              'tags':labels,'available':not(category.get('isHidden') or item.get('isHidden') or size.get('isHidden')),
                              'photo_url':str(size.get('buttonImageUrl') or '')}
                    if identity in seen:
                        raise ValueError('Один UUID и размер повторяются в меню; уберите дубли перед импортом')
                    seen.add(identity); rows.append(record)
            except (ValueError, InvalidOperation, TypeError) as exc:
                # Reject ambiguous structure rather than applying a partially parsed item.
                raise ValueError(f'{title or "Блюдо"}: {exc}') from None
    if len(rows)>1000:
        raise ValueError('Максимум 1000 размеров блюд за один импорт')
    if not rows and not allow_empty:
        details = '; '.join(skipped[:5]) or 'Внешнее меню не содержит блюд в itemCategories'
        raise ValueError('Нет поддерживаемых позиций с ценой для выбранной организации. ' + details)
    if menu.get('comboCategories'):
        skipped.append('Комбо-категории не импортируются')
    return {'name':str(menu.get('name') or ''),'source':source_key(cfg),'rows':rows,'skipped':skipped}


def prepare_preview(c, data):
    products = [dict(p) for p in c.execute('SELECT * FROM products')]
    matched = set()
    for row in data['rows']:
        existing = [p for p in products if p['iiko_id']==row['iiko_id'] and p['iiko_size']==row['iiko_size']]
        if len(existing)>1:
            raise ValueError(f'{row["name"]}: несколько местных карточек с одинаковыми UUID')
        old = existing[0] if existing else None
        if old and old['iiko_source'] not in ('',data['source']):
            raise ValueError(f'{row["name"]}: карточка уже связана с другим меню/организацией')
        row['local_id'] = old['id'] if old else None
        row['old_price'] = old['price'] if old else None
        if old:
            matched.add(old['id'])
        row['photo_supported'] = photo_allowed(row['photo_url'])
    data['hide_ids'] = [p['id'] for p in products if p['iiko_source']==data['source'] and p['id'] not in matched and p['active']]
    return data


def diagnostic(menu, cfg):
    """Allowlisted menu data only: no credentials, orders, tokens, or raw response."""
    samples = []
    counts = Counter()
    for category in menu.get('itemCategories') or []:
        for item in category.get('items') or []:
            counts['items'] += 1
            counts['type:' + str(item.get('type'))] += 1
            counts['orderItemType:' + str(item.get('orderItemType'))] += 1
            if len(samples) >= 30:
                continue
            sizes = []
            for size in (item.get('itemSizes') or [])[:20]:
                groups = []
                for group in (size.get('itemModifierGroups') or [])[:20]:
                    groups.append({'name':group.get('name'), 'optionalWithoutDefaults':optional_modifiers([group])})
                sizes.append({'sizeId':size.get('sizeId'),'sizeName':size.get('sizeName'),
                    'prices':[{'organizations':p.get('organizations'), 'price':p.get('price')} for p in (size.get('prices') or [])[:20]],
                    'modifierGroups':groups})
            samples.append({**{k:item.get(k) for k in ('itemId','name','type','orderItemType','canBeDivided','canSetOpenPrice','isMarked')},
                            'category':category.get('name'),'categoryHasSchedule':bool(category.get('schedules') or category.get('scheduleId')),
                            'sizes':sizes})
    return {'selected':{k:cfg.get(k) for k in ('organization_id','external_menu','price_category')},
            'formatVersion':menu.get('formatVersion'),'menuId':menu.get('id'),
            'menuHasSchedule':bool(menu.get('intervals')), 'counts':dict(counts),'first30Items':samples}


@router.post('/admin/integrations/iiko/menu-diagnostic')
async def menu_diagnostic(request: Request):
    from .admin import form_data, redirect
    await form_data(request)
    cfg = get_config('iiko')
    try:
        body = {'externalMenuId':cfg['external_menu'],'organizationIds':[cfg['organization_id']],'version':2}
        if cfg.get('price_category'):
            body['priceCategoryId'] = cfg['price_category']
        menu = await iiko_call(cfg,'/api/2/menu/by_id',body)
        return JSONResponse(diagnostic(menu,cfg),headers={'Content-Disposition':'attachment; filename="iiko-menu-diagnostic.json"'})
    except Exception:
        return redirect('/admin/integrations',error='Не удалось получить диагностику меню. Проверьте сохранённые доступы и ID меню.')


def apply_import(token):
    with db.connect(True) as c:
        batch = c.execute('SELECT * FROM menu_imports WHERE token=?',(token,)).fetchone()
        if not batch or time.time()-batch['created']>1800:
            raise ValueError('Предпросмотр устарел. Загрузите меню заново.')
        if batch['result']:
            return json.loads(batch['result'])
        data = json.loads(batch['payload'])
        if source_key(get_config('iiko',c)) != data['source']:
            raise ValueError('Настройки меню изменились. Повторите загрузку.')
        if catalog_fingerprint(c) != batch['fingerprint']:
            raise ValueError('Каталог изменился после предпросмотра. Загрузите меню ещё раз.')
        counts = {'created':0,'updated':0,'hidden':len(data['hide_ids']),'photos':0}
        for row in data['rows']:
            c.execute('INSERT OR IGNORE INTO categories(name) VALUES (?)',(row['category'],))
            category_id = c.execute('SELECT id FROM categories WHERE name=?',(row['category'],)).fetchone()[0]
            if row['local_id']:
                # Preserve local composition, photography, descriptive text and publication.
                c.execute('UPDATE products SET category_id=?,name=?,price=?,weight=?,iiko_source=?,active=CASE WHEN ? THEN active ELSE 0 END WHERE id=?',
                          (category_id,row['name'],row['price'],row['weight'],data['source'],row['available'],row['local_id']))
                pid = row['local_id']; counts['updated']+=1
                if not row['available']:
                    counts['hidden']+=1
            else:
                pid = c.execute("""INSERT INTO products(category_id,name,price,weight,description,ingredients,tags,active,iiko_id,iiko_size,iiko_source)
                    VALUES (?,?,?,?,?,'',?,0,?,?,?)""",
                    (category_id,row['name'],row['price'],row['weight'],row['description'],row['tags'],row['iiko_id'],row['iiko_size'],data['source'])).lastrowid
                counts['created']+=1
            current = c.execute('SELECT photo FROM products WHERE id=?',(pid,)).fetchone()[0]
            if not current and row['photo_supported']:
                c.execute("INSERT INTO menu_images(product_id,url,source) VALUES (?,?,?) ON CONFLICT(product_id) DO UPDATE SET url=excluded.url,source=excluded.source,attempts=0,state='pending'", (pid,row['photo_url'],data['source']))
                counts['photos']+=1
        c.executemany('UPDATE products SET active=0 WHERE id=?',[(pid,) for pid in data['hide_ids']])
        c.execute('UPDATE menu_imports SET result=? WHERE token=?',(json.dumps(counts),token))
        return counts


@router.post('/admin/integrations/iiko/menu-preview')
async def menu_preview(request: Request):
    from .admin import form_data,render,redirect
    await form_data(request)
    cfg = get_config('iiko')
    try:
        if not all(cfg.get(k) for k in ('api_key','app_id','client_secret','organization_id','external_menu')):
            raise ValueError('Сохраните API key, appId, clientSecret, организацию и ID внешнего меню')
        body = {'externalMenuId':cfg['external_menu'],'organizationIds':[cfg['organization_id']],'version':2}
        if cfg.get('price_category'):
            body['priceCategoryId'] = cfg['price_category']
        menu = await iiko_call(cfg,'/api/2/menu/by_id',body)
        data = normalize_menu(menu,cfg,allow_empty=True)
        if not data['rows']:
            return render(request,'menu_preview.html',batch=data,import_token='',page='integrations')
        with db.connect(True) as c:
            data = prepare_preview(c,data)
            token = secrets.token_urlsafe(32)
            c.execute('DELETE FROM menu_imports WHERE created<?',(time.time()-86400,))
            c.execute('INSERT INTO menu_imports(token,payload,fingerprint,created) VALUES (?,?,?,?)',
                      (token,json.dumps(data,ensure_ascii=False),catalog_fingerprint(c),time.time()))
        return render(request,'menu_preview.html',batch=data,import_token=token,page='integrations')
    except ValueError as exc:
        return redirect('/admin/integrations',error=exc)
    except Exception:
        return redirect('/admin/integrations',error='Не удалось загрузить внешнее меню. Проверьте доступы и ID меню. Каталог не изменён.')


@router.post('/admin/integrations/iiko/menu-apply')
async def menu_apply(request: Request):
    from .admin import form_data,redirect
    form = await form_data(request)
    try:
        result = apply_import(str(form.get('import_token','')))
        return redirect('/admin/products',ok=f"Импорт завершён: создано {result['created']}, обновлено {result['updated']}, скрыто {result['hidden']}. Фото в очереди: {result['photos']}. Проверьте состав и опубликуйте новые карточки.")
    except ValueError as exc:
        return redirect('/admin/integrations',error=exc)


async def load_image(row):
    from .admin import save_photo
    filename = None
    try:
        if not photo_allowed(row['url']):
            raise ValueError('Unsupported image host')
        content = bytearray()
        async with httpx.AsyncClient(timeout=10,follow_redirects=False,trust_env=False) as client:
            async with client.stream('GET',row['url']) as response:
                if response.status_code != 200:
                    raise ValueError('Image unavailable')
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content)>5*1024*1024:
                        raise ValueError('Image too large')
        filename = await save_photo(UploadFile(io.BytesIO(content),filename='menu-image'))
        with db.connect(True) as c:
            saved = c.execute("UPDATE products SET photo=? WHERE id=? AND photo='' AND iiko_source=? AND EXISTS (SELECT 1 FROM menu_images WHERE product_id=? AND url=?)",(filename,row['product_id'],row['source'],row['product_id'],row['url'])).rowcount
            c.execute("UPDATE menu_images SET state='done' WHERE product_id=? AND url=?",(row['product_id'],row['url']))
        if not saved:
            (config.MEDIA/filename).unlink(missing_ok=True)
    except Exception:
        if filename:
            (config.MEDIA/filename).unlink(missing_ok=True)
        with db.connect(True) as c:
            c.execute("UPDATE menu_images SET state='failed' WHERE product_id=? AND attempts>=3",(row['product_id'],))


async def image_tick():
    with db.connect(True) as c:
        c.execute("UPDATE menu_images SET state='failed' WHERE state='pending' AND attempts>=3")
        rows = [dict(r) for r in c.execute("SELECT * FROM menu_images WHERE state='pending' AND attempts<3 LIMIT 2")]
        c.executemany('UPDATE menu_images SET attempts=attempts+1 WHERE product_id=?',[(r['product_id'],) for r in rows])
    if rows:
        await asyncio.gather(*(asyncio.wait_for(load_image(row),timeout=20) for row in rows),return_exceptions=True)
