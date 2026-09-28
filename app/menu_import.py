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
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile

from . import config, db
from .integrations import iiko_call, uuid_field
from .vault import get_config

router = APIRouter()


def menu_failure(exc, cfg, stage):
    """Share failure metadata, never response bodies, credentials or exception text."""
    report = {'stage':stage, 'exceptionType':type(exc).__name__}
    locations = []
    trace = exc.__traceback__
    while trace:
        file = Path(trace.tb_frame.f_code.co_filename)
        if file.parent == Path(__file__).parent:
            locations.append({'file':file.name, 'line':trace.tb_lineno,
                              'function':trace.tb_frame.f_code.co_name})
        trace = trace.tb_next
    report['locations'] = locations[-6:]
    if isinstance(exc, httpx.HTTPStatusError):
        report['httpStatus'] = exc.response.status_code
        path = exc.request.url.path
        report['stage'] = 'authorization' if path == '/api/v2/access_token' else stage
        try:
            payload = exc.response.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            code = payload.get('errorCode') or payload.get('code') or payload.get('error')
            secrets_in_config = [str(cfg[k]) for k in ('api_key','client_secret','app_id') if cfg.get(k)]
            if isinstance(code, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,79}', code) and not any(s in code for s in secrets_in_config):
                report['errorCode'] = code
            try:
                correlation = uuid_field(payload.get('correlationId') or '')
                if correlation and correlation not in secrets_in_config:
                    report['correlationId'] = correlation
            except (ValueError, TypeError, AttributeError):
                pass
        hint = {
            400:'iiko отклонила параметры запроса. Проверьте ID меню, организацию и ценовую категорию.',
            401:'iiko отклонила авторизацию.',
            403:'iiko запретила доступ к запрошенным данным.',
            404:'Запрошенные данные или метод iiko не найдены.',
            429:'Превышен лимит запросов iiko. Повторите позже.',
        }.get(exc.response.status_code, 'iiko вернула ошибку HTTP.')
        label = 'авторизация' if report['stage'] == 'authorization' else 'загрузка меню'
        message = f"{hint} HTTP {report['httpStatus']}. Этап: {label}."
        if report.get('errorCode'):
            message += f" Код iiko: {report['errorCode']}."
    elif isinstance(exc, httpx.TimeoutException):
        message = 'Не дождались ответа iiko. Повторите позже.'
    elif isinstance(exc, httpx.RequestError):
        message = 'VPS не смог выполнить сетевой запрос к iiko.'
    else:
        label = {'menu_request':'загрузка меню', 'menu_processing':'обработка ответа iiko',
                 'catalog_preview':'подготовка каталога'}.get(stage, 'проверка меню')
        message = f"Ошибка на этапе «{label}»: {report['exceptionType']}."
    return report, message + ' Каталог не изменён. Скачайте диагностику меню для разбора ошибки.'


def source_key(cfg):
    return '|'.join(str(cfg.get(k) or '') for k in ('organization_id', 'external_menu', 'price_category'))


def catalog_fingerprint(c):
    snapshot = [[dict(r) for r in c.execute('SELECT * FROM products ORDER BY id')],
                [dict(r) for r in c.execute('SELECT * FROM categories ORDER BY id')]]
    return hashlib.sha256(json.dumps(snapshot,sort_keys=True).encode()).hexdigest()


def photo_allowed(url):
    try:
        p = urlsplit(url)
        # iiko uses numeric CDN hosts and UUID-named Selectel storage hosts.
        # Keep this allowlist narrow: no arbitrary origins, credentials or redirects.
        host = p.hostname or ''
        supported = (re.fullmatch(r'\d+\.selcdn\.ru', host)
                     or re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\.selstorage\.ru', host))
        return p.scheme == 'https' and bool(supported) and p.port in (None,443) and not p.username and not p.password
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


def price_organizations(record):
    """Accept grouped prices and the per-organization shape observed in live v2."""
    singular = record.get('organizationId')
    grouped = record.get('organizations')
    if singular is not None and not isinstance(singular, str):
        return []
    if grouped is not None and (not isinstance(grouped, list) or
                                not all(isinstance(value, str) for value in grouped)):
        return []
    organizations = [value.lower() for value in grouped or []]
    if singular is not None:
        # Never resolve conflicting identifiers by silently preferring one shape.
        if grouped is not None and set(organizations) != {singular.lower()}:
            return []
        return [singular.lower()]
    return organizations


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
                    prices = [p.get('price') for p in size.get('prices') or []
                              if cfg['organization_id'].lower() in price_organizations(p)]
                    if not prices:
                        skipped.append(title + ': нет записи цены для выбранной организации; проверьте ID организации')
                        continue
                    if len(prices) != 1:
                        skipped.append(title + ': несколько записей цены для выбранной организации; проверьте меню iiko')
                        continue
                    if prices[0] is None:
                        skipped.append(title + ': iiko вернула цену null для выбранной организации; проверьте категорию цен и доступность блюда в iiko')
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


def prepare_preview(c, data, replace_source=None):
    if replace_source and replace_source.split('|')[0] != data['source'].split('|')[0]:
        raise ValueError('Переключение допускается только между меню одной организации.')
    products = [dict(p) for p in c.execute('SELECT * FROM products')]
    matched = set()
    for row in data['rows']:
        existing = [p for p in products if p['iiko_id']==row['iiko_id'] and p['iiko_size']==row['iiko_size']]
        if len(existing)>1:
            raise ValueError(f'{row["name"]}: несколько местных карточек с одинаковыми UUID')
        old = existing[0] if existing else None
        if old and old['iiko_source'] not in ('',data['source'],replace_source):
            raise ValueError(f'{row["name"]}: карточка уже связана с другим меню/организацией')
        row['local_id'] = old['id'] if old else None
        row['old_price'] = old['price'] if old else None
        if old:
            matched.add(old['id'])
        row['photo_supported'] = photo_allowed(row['photo_url'])
    data['hide_ids'] = [p['id'] for p in products if p['iiko_source'] in (data['source'],replace_source) and p['id'] not in matched and (p['iiko_available'] or p['active'])]
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
                    'prices':[{key:p[key] for key in ('organizationId','organizations','price') if key in p}
                              for p in (size.get('prices') or [])[:20]],
                    'modifierGroups':groups})
            samples.append({**{k:item.get(k) for k in ('itemId','name','type','orderItemType','canBeDivided','canSetOpenPrice','isMarked')},
                            'category':category.get('name'),'categoryHasSchedule':bool(category.get('schedules') or category.get('scheduleId')),
                            'sizes':sizes})
    return {'selected':{k:cfg.get(k) for k in ('organization_id','external_menu','price_category')},
            'formatVersion':menu.get('formatVersion'),'menuId':menu.get('id'),
            'menuHasSchedule':bool(menu.get('intervals')), 'counts':dict(counts),'first30Items':samples}


@router.post('/admin/integrations/iiko/menu-diagnostic')
async def menu_diagnostic(request: Request):
    from .admin import form_data
    await form_data(request)
    cfg = get_config('iiko')
    stage = 'menu_request'
    try:
        body = {'externalMenuId':cfg['external_menu'],'organizationIds':[cfg['organization_id']],'version':2}
        if cfg.get('price_category'):
            body['priceCategoryId'] = cfg['price_category']
        menu = await iiko_call(cfg,'/api/2/menu/by_id',body)
        stage = 'menu_processing'
        result = diagnostic(menu,cfg)
        try:
            normalized = normalize_menu(menu,cfg,allow_empty=True)
            result['preview'] = {'importable':len(normalized['rows']), 'remarks':normalized['skipped']}
        except Exception as exc:
            result['failure'], _ = menu_failure(exc,cfg,stage)
    except Exception as exc:
        failure, _ = menu_failure(exc,cfg,stage)
        result = {'selected':{k:cfg.get(k) for k in ('organization_id','external_menu','price_category')},
                  'failure':failure}
    return JSONResponse(result,headers={'Content-Disposition':'attachment; filename="iiko-menu-diagnostic.json"'})


def apply_rows(c, data):
    """Caller holds the write transaction and has prepared a fresh preview."""
    if not data['rows']:
        raise ValueError('Пустое меню: каталог сохранён без изменений.')
    counts = {'created':0,'updated':0,'hidden':0,'photos':0}
    for row in data['rows']:
        c.execute('INSERT OR IGNORE INTO categories(name) VALUES (?)',(row['category'],))
        category = c.execute('SELECT id FROM categories WHERE name=?',(row['category'],)).fetchone()[0]
        pid = row['local_id']
        if pid:
            old = c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone()
            active = bool(old['active'] or old['iiko_resume_active']) if row['available'] else False
            resume = 0 if row['available'] else int(old['active'] or old['iiko_resume_active'])
            counts['hidden'] += int(bool(old['active']) and not active)
            c.execute('UPDATE products SET category_id=?,name=?,price=?,weight=?,iiko_source=?,active=?,iiko_available=?,iiko_resume_active=? WHERE id=?',
                (category,row['name'],row['price'],row['weight'],data['source'],int(active),int(row['available']),resume,pid))
            counts['updated'] += 1
        else:
            pid = c.execute("""INSERT INTO products(category_id,name,price,weight,description,ingredients,tags,active,iiko_id,iiko_size,iiko_source,iiko_available)
                VALUES (?,?,?,?,?,'',?,0,?,?,?,?)""",
                (category,row['name'],row['price'],row['weight'],row['description'],row['tags'],row['iiko_id'],row['iiko_size'],data['source'],int(row['available']))).lastrowid
            counts['created'] += 1
        current = c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone()
        if (not current['photo'] or current['photo']==current['iiko_photo']) and row['photo_url']:
            # Refresh managed images, including replacements served at the same URL.
            # Do not invalidate a download already running for this exact source.
            job = c.execute('SELECT * FROM menu_images WHERE product_id=?',(pid,)).fetchone()
            if not job or job['state']!='downloading' or job['url']!=row['photo_url'] or job['source']!=data['source']:
                state = 'pending' if row['photo_supported'] else 'failed'
                error = '' if row['photo_supported'] else 'Источник фото пока не поддерживается: ' + (urlsplit(row['photo_url']).hostname or 'неверный адрес')
                c.execute("""INSERT INTO menu_images(product_id,url,source,state,error,job_token) VALUES (?,?,?,?,?,?)
                    ON CONFLICT(product_id) DO UPDATE SET
                    etag=CASE WHEN url=excluded.url AND source=excluded.source THEN etag ELSE '' END,
                    last_modified=CASE WHEN url=excluded.url AND source=excluded.source THEN last_modified ELSE '' END,
                    url=excluded.url,source=excluded.source,attempts=0,
                    state=excluded.state,error=excluded.error,job_token=excluded.job_token,started=0""",
                    (pid,row['photo_url'],data['source'],state,error,secrets.token_hex(16)))
                counts['photos'] += int(row['photo_supported'])
    for pid in data['hide_ids']:
        counts['hidden'] += c.execute('SELECT active FROM products WHERE id=?',(pid,)).fetchone()[0]
        c.execute('UPDATE products SET iiko_resume_active=MAX(active,iiko_resume_active),active=0,iiko_available=0 WHERE id=?',(pid,))
    return counts


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
        counts = apply_rows(c, data)
        c.execute('UPDATE menu_imports SET result=? WHERE token=?',(json.dumps(counts),token))
        return counts


@router.post('/admin/integrations/iiko/menu-preview')
async def menu_preview(request: Request):
    from .admin import form_data,render,redirect
    await form_data(request)
    cfg = get_config('iiko')
    stage = 'menu_request'
    try:
        if not all(cfg.get(k) for k in ('api_key','app_id','client_secret','organization_id','external_menu')):
            raise ValueError('Сохраните API key, appId, clientSecret, организацию и ID внешнего меню')
        body = {'externalMenuId':cfg['external_menu'],'organizationIds':[cfg['organization_id']],'version':2}
        if cfg.get('price_category'):
            body['priceCategoryId'] = cfg['price_category']
        menu = await iiko_call(cfg,'/api/2/menu/by_id',body)
        stage = 'menu_processing'
        data = normalize_menu(menu,cfg,allow_empty=True)
        if not data['rows']:
            return render(request,'menu_preview.html',batch=data,import_token='',page='integrations')
        stage = 'catalog_preview'
        with db.connect(True) as c:
            data = prepare_preview(c,data)
            token = secrets.token_urlsafe(32)
            c.execute('DELETE FROM menu_imports WHERE created<?',(time.time()-86400,))
            c.execute('INSERT INTO menu_imports(token,payload,fingerprint,created) VALUES (?,?,?,?)',
                      (token,json.dumps(data,ensure_ascii=False),catalog_fingerprint(c),time.time()))
        return render(request,'menu_preview.html',batch=data,import_token=token,page='integrations')
    except ValueError as exc:
        return redirect('/admin/integrations',error=exc)
    except Exception as exc:
        _, message = menu_failure(exc,cfg,stage)
        return redirect('/admin/integrations',error=message)


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
    committed = False
    try:
        if not photo_allowed(row['url']):
            raise ValueError('Источник фото пока не поддерживается')
        with db.connect() as c:
            current = c.execute('SELECT photo,iiko_photo,iiko_photo_url FROM products WHERE id=?',(row['product_id'],)).fetchone()
        conditional = {}
        if (current and current['photo'] == current['iiko_photo'] and current['iiko_photo_url']==row['url']
                and re.fullmatch(r'[a-f0-9]{32}\.jpg',current['photo']) and (config.MEDIA/current['photo']).is_file()):
            if row.get('etag'):
                conditional['If-None-Match'] = row['etag']
            if row.get('last_modified'):
                conditional['If-Modified-Since'] = row['last_modified']
        content = bytearray()
        async with httpx.AsyncClient(timeout=10,follow_redirects=False,trust_env=False) as client:
            async with client.stream('GET',row['url'],headers=conditional) as response:
                if response.status_code == 304 and conditional:
                    with db.connect(True) as c:
                        c.execute("UPDATE menu_images SET state='done',error='' WHERE product_id=? AND job_token=?",(row['product_id'],row['job_token']))
                    return
                if response.status_code != 200:
                    raise ValueError(f'Сервер фото вернул HTTP {response.status_code}')
                headers = getattr(response,'headers',{})
                etag = headers.get('etag','')[:500]
                last_modified = headers.get('last-modified','')[:100]
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content)>5*1024*1024:
                        raise ValueError('Фото больше 5 МБ')
        filename = await save_photo(UploadFile(io.BytesIO(content),filename='menu-image'))
        with db.connect(True) as c:
            old = c.execute('SELECT iiko_photo FROM products WHERE id=?',(row['product_id'],)).fetchone()
            saved = c.execute("""UPDATE products SET photo=?,iiko_photo=?,iiko_photo_url=?
                WHERE id=? AND (photo='' OR photo=iiko_photo) AND iiko_source=?
                AND EXISTS (SELECT 1 FROM menu_images WHERE product_id=? AND job_token=?)""",
                (filename,filename,row['url'],row['product_id'],row['source'],row['product_id'],row['job_token'])).rowcount
            c.execute("UPDATE menu_images SET state='done',error='',etag=?,last_modified=? WHERE product_id=? AND job_token=?",(etag,last_modified,row['product_id'],row['job_token']))
        committed = bool(saved)
        if not saved:
            (config.MEDIA/filename).unlink(missing_ok=True)
        elif old and old['iiko_photo'] and old['iiko_photo'] != filename:
            previous = old['iiko_photo']
            if re.fullmatch(r'[a-f0-9]{32}\.jpg', previous):
                with db.connect() as c:
                    used = (c.execute('SELECT 1 FROM products WHERE photo=?',(previous,)).fetchone()
                            or c.execute('SELECT 1 FROM slides WHERE photo=?',(previous,)).fetchone()
                            or c.execute('SELECT 1 FROM settings WHERE value=?',(previous,)).fetchone())
                if not used:
                    (config.MEDIA/previous).unlink(missing_ok=True)
    except Exception as exc:
        if filename and not committed:
            (config.MEDIA/filename).unlink(missing_ok=True)
        message = str(exc) if type(exc) is ValueError else type(exc).__name__ + ': не удалось загрузить изображение'
        with db.connect(True) as c:
            c.execute("UPDATE menu_images SET state=CASE WHEN attempts>=3 THEN 'failed' ELSE 'pending' END,error=? WHERE product_id=? AND job_token=?",
                      (message[:250],row['product_id'],row['job_token']))


async def image_tick():
    with db.connect(True) as c:
        c.execute("UPDATE menu_images SET state='pending' WHERE state='downloading' AND started<?",(time.time()-120,))
        c.execute("UPDATE menu_images SET state='failed' WHERE state='pending' AND attempts>=3")
        rows = [dict(r) for r in c.execute("SELECT * FROM menu_images WHERE state='pending' AND attempts<3 ORDER BY attempts,product_id LIMIT 2")]
        for row in rows:
            row['job_token'] = secrets.token_hex(16)
            c.execute("UPDATE menu_images SET attempts=attempts+1,state='downloading',started=?,job_token=? WHERE product_id=?",
                      (time.time(),row['job_token'],row['product_id']))
    if rows:
        await asyncio.gather(*(asyncio.wait_for(load_image(row),timeout=30) for row in rows),return_exceptions=True)
