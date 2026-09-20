import json
import re
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request

from . import config, db
from .integrations import TAXATIONS, TAXES, bank_webhook, iiko_call, uuid_field
from .vault import get_config, set_config

router = APIRouter()
router.add_api_route('/api/payments/tbank/webhook', bank_webhook, methods=['POST'])


@router.get('/admin/appearance')
def appearance(request: Request):
    from .admin import render, require_admin
    require_admin(request)
    with db.connect() as c:
        slides = c.execute('SELECT * FROM slides ORDER BY position,id').fetchall()
    return render(request, 'appearance.html', slides=slides, page='appearance')


@router.post('/admin/appearance')
async def save_appearance(request: Request):
    from .admin import field, form_data, redirect, save_photo
    form = await form_data(request)
    filename = None
    try:
        values = {k: field(form,k,n,False) for k,n in [('tagline',160),('delivery_area',500),('legal_name',200),('legal_details',2000),('privacy_text',15000),('offer_text',15000)]}
        values['shop_name'] = field(form,'shop_name',80)
        mode = field(form,'hero_mode',10)
        if mode not in ('static','carousel'):
            raise ValueError('Выберите тип первого экрана')
        values['hero_mode'] = mode
        if form.get('remove_logo'):
            values['logo'] = ''
        upload = form.get('photo')
        if upload and getattr(upload,'filename',''):
            filename = await save_photo(upload)
            values['logo'] = filename
        with db.connect(True) as c:
            c.executemany('UPDATE settings SET value=? WHERE key=?', [(v,k) for k,v in values.items()])
        return redirect('/admin/appearance',ok='Оформление сохранено')
    except ValueError as exc:
        if filename:
            (config.MEDIA/filename).unlink(missing_ok=True)
        return redirect('/admin/appearance',error=exc)
    finally:
        await form.close()


@router.post('/admin/slides')
async def save_slide(request: Request):
    from .admin import field, form_data, redirect, save_photo
    form = await form_data(request)
    filename = None
    try:
        sid = int(form.get('id') or 0)
        with db.connect() as c:
            old = c.execute('SELECT * FROM slides WHERE id=?',(sid,)).fetchone()
        if sid and not old:
            raise ValueError('Слайд не найден')
        if form.get('delete'):
            with db.connect(True) as c:
                c.execute('DELETE FROM slides WHERE id=?',(sid,))
            return redirect('/admin/appearance',ok='Слайд удалён')
        target = field(form,'target',150)
        if not re.fullmatch(r'#(?:catalog|category-\d+)',target):
            raise ValueError('Кнопка может вести на #catalog или #category-ID')
        title, subtitle, button = field(form,'title',100),field(form,'subtitle',400,False),field(form,'button',40)
        position = int(form.get('position') or 0)
        photo = old['photo'] if old else ''
        if form.get('remove_photo'):
            photo = ''
        upload = form.get('photo')
        if upload and getattr(upload,'filename',''):
            filename = await save_photo(upload)
            photo = filename
        values = (title,subtitle,button,target,photo,position,int(form.get('active')=='on'))
        with db.connect(True) as c:
            if sid:
                c.execute('UPDATE slides SET title=?,subtitle=?,button=?,target=?,photo=?,position=?,active=? WHERE id=?',(*values,sid))
            else:
                if c.execute('SELECT count(*) FROM slides').fetchone()[0] >= 12:
                    raise ValueError('Максимум 12 слайдов')
                c.execute('INSERT INTO slides(title,subtitle,button,target,photo,position,active) VALUES (?,?,?,?,?,?,?)',values)
        return redirect('/admin/appearance',ok='Слайд сохранён')
    except ValueError as exc:
        if filename:
            (config.MEDIA/filename).unlink(missing_ok=True)
        return redirect('/admin/appearance',error=exc)
    finally:
        await form.close()


SECRET_FIELDS = {'telegram': ('token',), 'tbank': ('password',), 'iiko': ('api_key','client_secret')}


@router.get('/admin/integrations')
def integrations_page(request: Request):
    from .admin import render, require_admin
    require_admin(request)
    configs = {}
    for name, secrets in SECRET_FIELDS.items():
        value = get_config(name)
        configs[name] = {k:v for k,v in value.items() if k not in secrets}
        configs[name]['configured_secrets'] = {k: bool(value.get(k) or (name=='telegram' and k=='token' and config.BOT_TOKEN)) for k in secrets}
    if 'admin_ids' not in configs['telegram']:
        configs['telegram']['admin_ids'] = config.ADMIN_IDS
    with db.connect() as c:
        jobs = c.execute('SELECT order_id,state,error,attempts FROM iiko_jobs ORDER BY order_id DESC LIMIT 30').fetchall()
        payments = c.execute('SELECT order_id,state,error,attempts FROM payments ORDER BY order_id DESC LIMIT 30').fetchall()
        image_counts = dict(c.execute('SELECT state,count(*) FROM menu_images GROUP BY state').fetchall())
    return render(request,'integrations.html',configs=configs,taxes=TAXES,taxations=TAXATIONS,jobs=jobs,payments=payments,image_counts=image_counts,page='integrations')


@router.post('/admin/integrations/{name}')
async def save_integration(request: Request, name: str):
    from .admin import field, form_data, redirect
    if name not in SECRET_FIELDS:
        raise HTTPException(404)
    form = await form_data(request)
    try:
        value = get_config(name)
        value['enabled'] = form.get('enabled') == 'on'
        for k in SECRET_FIELDS[name]:
            secret = field(form,k,512,False)
            if secret:
                value[k] = secret
        if name=='telegram':
            ids = field(form,'admin_ids',500,False)
            if ids and any(not re.fullmatch(r'-?\d{1,16}',part.strip()) for part in ids.split(',')):
                raise ValueError('Введите числовые ID чатов через запятую')
            value['admin_ids'] = [int(part.strip()) for part in ids.split(',') if part.strip()]
            token = value.get('token') or config.BOT_TOKEN
            if token and not re.fullmatch(r'\d+:[A-Za-z0-9_-]{20,}',token):
                raise ValueError('Неверный формат токена Telegram')
            if value['enabled'] and (not token or not value['admin_ids']):
                raise ValueError('Нужен токен и хотя бы один ID администратора')
        elif name=='tbank':
            for k in ('terminal','public_url','tax','delivery_tax','taxation'):
                value[k] = field(form,k,250,False)
            value['fiscal_ready'] = form.get('fiscal_ready')=='on'
            parsed = urlparse(value['public_url'])
            if value['public_url'] and (parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.query or parsed.fragment or parsed.path not in ('','/')):
                raise ValueError('Публичный URL: https://ваш-домен без пути и параметров')
            if value['enabled'] and not all(value.get(k) for k in ('terminal','password','public_url','fiscal_ready')):
                raise ValueError('Заполните терминал, пароль, домен и подтвердите готовность кассы')
            if value['enabled'] and (value['tax'] not in TAXES or value['delivery_tax'] not in TAXES or value['taxation'] not in TAXATIONS):
                raise ValueError('Укажите налогообложение и ставки НДС')
        else:
            for k in ('app_id','organization_id','terminal_group','delivery_product','payment_type'):
                value[k] = uuid_field(field(form,k,36,False))
            value['city_format'] = form.get('city_format')=='on'
            value['external_menu'] = field(form,'external_menu',100,False)
            value['price_category'] = uuid_field(field(form,'price_category',36,False))
            if value['enabled'] and not all(value.get(k) for k in ('api_key','app_id','client_secret','organization_id','terminal_group')):
                raise ValueError('Для iiko нужны API key, appId, clientSecret, организация и терминал')
        set_config(name,value)
        return redirect('/admin/integrations',ok='Настройки сохранены. Новые заказы используют новые параметры.')
    except ValueError as exc:
        return redirect('/admin/integrations',error=exc)
    finally:
        await form.close()


@router.post('/admin/integrations/iiko/check')
async def check_iiko(request: Request):
    from .admin import form_data, render, redirect
    await form_data(request)
    cfg = get_config('iiko')
    try:
        organizations = await iiko_call(cfg,'/api/1/organizations',{'returnAdditionalInfo': True,'includeDisabled': False})
        menus = await iiko_call(cfg,'/api/2/menu',{})
        catalog = None
        groups = None
        payment_types = None
        if cfg.get('organization_id'):
            orgs = {'organizationIds':[cfg['organization_id']]}
            groups = await iiko_call(cfg,'/api/1/terminal_groups',orgs)
            payment_types = await iiko_call(cfg,'/api/1/payment_types',orgs)
            catalog = await iiko_call(cfg,'/api/1/nomenclature',{'organizationId':cfg['organization_id']})
        return render(request,'iiko_check.html',result=json.dumps({'organizations':organizations,'externalMenus':menus,'terminalGroups':groups,'paymentTypes':payment_types,'catalog':catalog},ensure_ascii=False,indent=2),page='integrations')
    except Exception:
        return redirect('/admin/integrations',error='Не удалось прочитать iiko. Проверьте ключи, права API и доступность сервиса.')


@router.post('/admin/integrations/telegram/check')
async def check_telegram(request: Request):
    from .admin import form_data, redirect
    from aiogram import Bot
    await form_data(request)
    cfg = get_config('telegram')
    try:
        async with Bot(cfg.get('token') or config.BOT_TOKEN) as bot:
            me = await bot.get_me()
        return redirect('/admin/integrations',ok=f'Подключён бот @{me.username}. Проверка getMe выполнена; сообщения не отправлялись.')
    except Exception:
        return redirect('/admin/integrations',error='Telegram не подтвердил токен')


@router.post('/admin/payments/{oid:int}/check')
async def check_payment(request: Request, oid: int):
    from .admin import form_data, redirect
    await form_data(request)
    with db.connect(True) as c:
        c.execute('UPDATE payments SET next_try=0,attempts=0 WHERE order_id=?',(oid,))
    return redirect('/admin/integrations',ok='Проверка запланирована. Новый платёж не создаётся.')


@router.post('/admin/iiko/{oid:int}/check')
async def check_job(request: Request, oid: int):
    from .admin import form_data, redirect
    await form_data(request)
    with db.connect(True) as c:
        c.execute("UPDATE iiko_jobs SET next_try=0,attempts=0 WHERE order_id=? AND state IN ('pending','checking')",(oid,))
    return redirect('/admin/integrations',ok='Проверка iiko запланирована')
