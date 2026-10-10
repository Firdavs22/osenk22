import hmac
import io
import secrets
import sqlite3
import warnings
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image, ImageOps, UnidentifiedImageError
from .mini_apps import ShopSessions

from . import config, db
from .security import verify_password

MAX_BODY = 6 * 1024 * 1024


class BodyTooLarge(Exception):
    pass


class RequestLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        size = 0

        async def limited_receive():
            nonlocal size
            event = await receive()
            size += len(event.get('body', b''))
            if size > MAX_BODY:
                raise BodyTooLarge()
            return event

        try:
            await self.app(scope, limited_receive, send)
        except BodyTooLarge:
            await send({'type': 'http.response.start', 'status': 413, 'headers': [(b'content-type', b'text/plain; charset=utf-8')]})
            await send({'type': 'http.response.body', 'body': 'Файл слишком большой. Максимум 5 МБ.'.encode()})


@asynccontextmanager
async def lifespan(app):
    if len(config.SESSION_SECRET) < 32 or not config.ADMIN_PASSWORD_HASH.startswith('scrypt$'):
        raise RuntimeError('Создайте SESSION_SECRET и ADMIN_PASSWORD_HASH: см. README.md')
    db.init()
    import asyncio
    from contextlib import suppress
    from .integrations import worker
    from .menu_sync import worker as menu_worker, image_worker
    from .max_bot import worker as max_worker
    tasks = [asyncio.create_task(fn()) for fn in (worker, menu_worker, image_worker, max_worker)]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(ShopSessions)
app.add_middleware(RequestLimit)
app.mount('/static', StaticFiles(directory=config.ROOT / 'app/static'), name='static')
templates = Jinja2Templates(directory=config.ROOT / 'app/templates')
templates.env.filters['money'] = db.money


@app.middleware('http')
async def headers(request: Request, call_next):
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    platform = request.session.get('mini_app')
    public_page = request.url.path=='/' or request.url.path.startswith(('/mini/','/order/','/legal/'))
    if public_page and platform in ('telegram','max'):
        sdk = 'https://telegram.org' if platform=='telegram' else 'https://st.max.ru'
        parents = 'https://web.telegram.org' if platform=='telegram' else 'https://web.max.ru https://max.ru'
        del response.headers['X-Frame-Options']
        response.headers['Content-Security-Policy'] = response.headers['Content-Security-Policy'].replace("script-src 'self'",f"script-src 'self' {sdk}").replace("frame-ancestors 'none'",f'frame-ancestors {parents}')
    if public_page and db.settings().get('metrika_enabled')=='1':
        policy = response.headers['Content-Security-Policy']
        policy = policy.replace("script-src 'self'", "script-src 'self' https://mc.yandex.ru https://yastatic.net")
        policy = policy.replace("connect-src 'self'", "connect-src 'self' https://mc.yandex.ru https://mc.yandex.com https://mc.webvisor.org")
        policy = policy.replace("img-src 'self' data:", "img-src 'self' data: https://mc.yandex.ru https://mc.yandex.com")
        response.headers['Content-Security-Policy'] = policy
    return response


def authenticated(request):
    return request.session.get('admin') == config.ADMIN_USERNAME and hmac.compare_digest(
        request.session.get('credential', ''), config.ADMIN_PASSWORD_HASH[-32:])


def require_admin(request):
    if not authenticated(request):
        raise HTTPException(303, headers={'Location': '/admin/login'})


def csrf_token(request):
    if 'csrf' not in request.session:
        request.session['csrf'] = secrets.token_urlsafe(32)
    return request.session['csrf']


async def form_data(request, admin=True, *, max_fields=30, max_files=1):
    if admin:
        require_admin(request)
    form = await request.form(max_files=max_files, max_fields=max_fields, max_part_size=MAX_BODY)
    expected = request.session.get('csrf')
    if not expected or not hmac.compare_digest(str(form.get('csrf', '')).encode(), expected.encode()):
        await form.close()
        raise HTTPException(403, 'Форма устарела. Обновите страницу и повторите действие.')
    return form


def render(request, name, **context):
    return templates.TemplateResponse(request=request, name=name, context={
        'csrf': csrf_token(request), 'shop': db.settings(), 'logged_in': authenticated(request),
        'mini_app': request.session.get('mini_app',''),
        'statuses': db.STATUSES, 'transitions': db.TRANSITIONS,
        'error': request.query_params.get('error', ''), 'ok': request.query_params.get('ok', ''), **context})


def redirect(path, error=None, ok=None):
    params = {'error': str(error)} if error else ({'ok': ok} if ok else {})
    return RedirectResponse(path + ('?' + urlencode(params) if params else ''), status_code=303)


def field(form, name, maximum=200, required=True):
    value = str(form.get(name, '')).strip()
    if (required and not value) or len(value) > maximum:
        requirement = 'обязательно, ' if required else ''
        raise ValueError(f'Поле «{name}»: {requirement}максимум {maximum} символов')
    return value


@app.get('/health')
def health():
    with db.connect() as c:
        c.execute('SELECT 1').fetchone()
    return {'status': 'ok'}


@app.get('/admin/login')
def login_page(request: Request):
    if authenticated(request):
        return redirect('/admin')
    return render(request, 'login.html')


@app.post('/admin/login')
async def login(request: Request):
    form = await form_data(request, admin=False)
    ip = request.client.host if request.client else 'unknown'
    if not db.login_allowed(ip):
        return redirect('/admin/login', error='Слишком много попыток. Повторите через 15 минут.')
    password_ok = verify_password(str(form.get('password', '')), config.ADMIN_PASSWORD_HASH)
    valid = hmac.compare_digest(str(form.get('username', '')).encode(), config.ADMIN_USERNAME.encode()) and password_ok
    db.login_result(ip, valid)
    if not valid:
        return redirect('/admin/login', error='Неверный логин или пароль')
    request.session.clear()
    request.session.update(admin=config.ADMIN_USERNAME, credential=config.ADMIN_PASSWORD_HASH[-32:], csrf=secrets.token_urlsafe(32))
    return redirect('/admin')


@app.post('/admin/logout')
async def logout(request: Request):
    await form_data(request)
    request.session.clear()
    return redirect('/admin/login')


@app.get('/admin')
def dashboard(request: Request):
    require_admin(request)
    with db.connect() as c:
        orders = c.execute('SELECT * FROM orders ORDER BY id DESC LIMIT 8').fetchall()
        counts = dict(c.execute('SELECT status,count(*) FROM orders GROUP BY status').fetchall())
        totals = c.execute("SELECT currency,sum(total) amount FROM orders WHERE status='done' GROUP BY currency").fetchall()
        product_count = c.execute('SELECT count(*) FROM products WHERE active=1').fetchone()[0]
        failed = c.execute('SELECT count(*) FROM outbox WHERE sent=-1').fetchone()[0]
    from .reporting import report
    return render(request, 'dashboard.html', report_summary=report({}), orders=orders, counts=counts, totals=totals, product_count=product_count, failed=failed, page='dashboard')


@app.get('/admin/products')
def products_page(request: Request):
    require_admin(request)
    return render(request, 'products.html', products=db.products(), categories=db.categories(), page='products')


@app.get('/admin/products/new')
def new_product(request: Request):
    require_admin(request)
    return render(request, 'product_form.html', product=None, categories=db.categories(), page='products')


@app.get('/admin/products/{pid:int}')
def edit_product(request: Request, pid: int):
    require_admin(request)
    p = db.product(pid)
    if not p:
        raise HTTPException(404, 'Товар не найден')
    return render(request, 'product_form.html', product=p, categories=db.categories(), page='products')


async def save_photo(upload):
    content = await upload.read(5*1024*1024+1)
    if len(content) > 5*1024*1024:
        raise ValueError('Фото должно быть не больше 5 МБ')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as source:
                if source.format not in ('JPEG', 'PNG', 'WEBP'):
                    raise ValueError('Поддерживаются JPEG, PNG и WebP')
                if source.width * source.height > 20_000_000:
                    raise ValueError('Фотография должна быть не больше 20 мегапикселей')
                source.load()
                img = ImageOps.exif_transpose(source).convert('RGB')
                img.thumbnail((1600, 1600))
                filename = secrets.token_hex(16) + '.jpg'
                img.save(config.MEDIA / filename, 'JPEG', quality=88)
                return filename
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError('Не удалось прочитать изображение. Загрузите JPEG, PNG или WebP.')


@app.post('/admin/products/save')
async def save_product(request: Request):
    form = await form_data(request)
    filename = None
    try:
        pid = int(form.get('id') or 0)
        old = db.product(pid) if pid else None
        if pid and not old:
            raise ValueError('Товар не найден')
        category = int(form.get('category_id', 0))
        name = field(form, 'name', 80)
        ingredients = field(form, 'ingredients', 1000)
        description = field(form, 'description', 1000, False)
        weight = field(form, 'weight', 60, False)
        tags = ', '.join(dict.fromkeys(t.strip() for t in field(form, 'tags', 160, False).split(',') if t.strip()))
        food_info = tuple(field(form,k,1000,False) for k in ('allergens','nutrition','storage'))
        from .integrations import uuid_field
        iiko_id = uuid_field(field(form, 'iiko_id', 36, False))
        iiko_size = uuid_field(field(form, 'iiko_size', 36, False))
        price = db.parse_money(form.get('price'), allow_zero=False)
        if category not in [r['id'] for r in db.categories()]:
            raise ValueError('Выберите существующую категорию')
        photo = old['photo'] if old else ''
        if form.get('remove_photo') == 'on':
            photo = ''
        upload = form.get('photo')
        if upload and getattr(upload, 'filename', ''):
            filename = await save_photo(upload)
            photo = filename
        values = (category, name, description, ingredients, weight, price, photo, int(form.get('active') == 'on'), tags, iiko_id, iiko_size)
        with db.connect(True) as c:
            if pid:
                current = c.execute('SELECT iiko_available FROM products WHERE id=?',(pid,)).fetchone()
                if form.get('active')=='on' and not current['iiko_available']:
                    raise ValueError('Блюдо недоступно в выбранном меню iiko. Сначала обновите меню.')
                c.execute('UPDATE products SET category_id=?,name=?,description=?,ingredients=?,weight=?,price=?,photo=?,active=?,tags=?,iiko_id=?,iiko_size=? WHERE id=?', (*values, pid))
                c.execute('UPDATE products SET iiko_resume_active=0 WHERE id=?',(pid,))
                if filename or form.get('remove_photo') == 'on':
                    c.execute('DELETE FROM menu_images WHERE product_id=?', (pid,))
                    c.execute("UPDATE products SET iiko_photo='',iiko_photo_url='' WHERE id=?",(pid,))
            else:
                pid = c.execute('INSERT INTO products(category_id,name,description,ingredients,weight,price,photo,active,tags,iiko_id,iiko_size) VALUES (?,?,?,?,?,?,?,?,?,?,?)', values).lastrowid
            c.execute('UPDATE products SET allergens=?,nutrition=?,storage=? WHERE id=?', (*food_info,pid))
        return redirect('/admin/products', ok='Товар сохранён')
    except (ValueError, sqlite3.IntegrityError) as exc:
        if filename:
            (config.MEDIA / filename).unlink(missing_ok=True)
        return redirect('/admin/products', error=exc)
    finally:
        await form.close()


@app.post('/admin/products/{pid:int}/toggle')
async def toggle_product(request: Request, pid: int):
    await form_data(request)
    with db.connect(True) as c:
        c.execute('UPDATE products SET active=CASE WHEN active=1 THEN 0 ELSE iiko_available END,iiko_resume_active=0 WHERE id=?', (pid,))
    return redirect('/admin/products')


@app.post('/admin/categories')
async def save_category(request: Request):
    form = await form_data(request)
    try:
        name = field(form, 'name', 50)
        with db.connect(True) as c:
            cid = int(form.get('id') or 0)
            if cid:
                c.execute('UPDATE categories SET name=? WHERE id=?', (name, cid))
            else:
                c.execute('INSERT INTO categories(name) VALUES (?)', (name,))
        return redirect('/admin/products', ok='Категория сохранена')
    except (ValueError, sqlite3.IntegrityError):
        return redirect('/admin/products', error='Введите уникальное название категории, до 50 символов')


@app.get('/admin/orders')
def orders_page(request: Request, status: str = '', page_num: int = 1, fragment: bool = False):
    require_admin(request)
    page_num = max(1, page_num)
    status = status if status in db.STATUSES else ''
    columns = {}
    start, end = request.query_params.get('start', ''), request.query_params.get('end', '')
    lo = hi = ''
    error = None
    if start or end:
        from .reporting import period
        try:
            _, _, lo, hi = period({'start': start or end, 'end': end or start})
            start, end = start or end, end or start
        except ValueError as exc:
            error = str(exc)
            lo = hi = '~'  # Invalid filters must not silently show unrelated orders.
    date_filter = " AND (?='' OR o.created_at>=?) AND (?='' OR o.created_at<?)"
    dates = (lo, lo, hi, hi)
    with db.connect() as c:
        counts = dict(c.execute('SELECT status,count(*) FROM orders o WHERE 1=1' + date_filter + ' GROUP BY status', dates).fetchall())
        for key in ([status] if status else db.STATUSES):
            columns[key] = c.execute('''SELECT o.*,
                (SELECT COALESCE(sum(quantity),0) FROM order_items WHERE order_id=o.id) item_count,
                (SELECT count(*) FROM orders customer_orders WHERE
                    (o.phone_key<>'' AND customer_orders.phone_key=o.phone_key)
                    OR (o.phone_key='' AND customer_orders.id=o.id)) customer_order_count
                FROM orders o WHERE status=?''' + date_filter + ' ORDER BY id DESC LIMIT 30 OFFSET ?',
                                     (key,*dates,(page_num-1)*30)).fetchall()
    return render(request, '_orders_board.html' if fragment else 'orders.html', columns=columns, counts=counts,
                  status=status, start=start, end=end, error=error, page_num=page_num, count=sum(counts.get(k,0) for k in columns),
                  has_more=any(counts.get(k,0)>page_num*30 for k in columns), page='orders')


@app.get('/admin/order-feed')
def order_feed(request: Request, after: int | None = None):
    require_admin(request)
    with db.connect() as c:
        latest = c.execute('SELECT COALESCE(max(id),0) FROM orders').fetchone()[0]
        rows = [] if after is None else c.execute('SELECT id FROM orders WHERE id>? ORDER BY id LIMIT 50',
                                                  (max(0,after),)).fetchall()
        pending = c.execute("SELECT count(*) FROM orders WHERE status='new'").fetchone()[0]
    return {'cursor':rows[-1]['id'] if rows else latest, 'orders':[r['id'] for r in rows], 'pending':pending}


@app.get('/admin/notifications-worker.js')
def notifications_worker():
    return FileResponse(config.ROOT / 'app/static/notifications-worker.js',media_type='text/javascript')


@app.get('/admin/orders/{oid:int}')
def order_page(request: Request, oid: int, fragment: bool = False):
    require_admin(request)
    with db.connect() as c:
        order = c.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
        items = c.execute('SELECT * FROM order_items WHERE order_id=?', (oid,)).fetchall()
    if not order:
        raise HTTPException(404, 'Заказ не найден')
    import json
    with db.connect() as c:
        a = c.execute('SELECT * FROM order_attribution WHERE order_id=?',(oid,)).fetchone()
        sync = c.execute('SELECT sync_status,sync_at,sync_error FROM iiko_jobs WHERE order_id=?',(oid,)).fetchone()
    attribution = [('Первый переход',json.loads(a['first_touch'])),('Последний рекламный переход',json.loads(a['last_touch']))] if a else []
    return render(request, '_order_details.html' if fragment else 'order.html', order=order, items=items, page='orders', attribution=attribution, iiko_sync=sync)


@app.post('/admin/orders/{oid:int}/status')
async def order_status(request: Request, oid: int):
    form = await form_data(request)
    try:
        db.set_status(oid, str(form.get('status')))
        if request.headers.get('accept') == 'application/json':
            return JSONResponse({'ok': True})
        return redirect(f'/admin/orders/{oid}', ok='Статус изменён. Уведомление поставлено в очередь.')
    except ValueError as exc:
        if request.headers.get('accept') == 'application/json':
            return JSONResponse({'detail': str(exc)}, status_code=400)
        return redirect(f'/admin/orders/{oid}', error=exc)


@app.get('/admin/settings')
def settings_page(request: Request):
    require_admin(request)
    return render(request, 'settings.html', page='settings')


@app.post('/admin/settings')
async def settings_save(request: Request):
    form = await form_data(request)
    try:
        values = {k: field(form, k, limit, required) for k, limit, required in [
            ('shop_name', 80, True), ('currency', 8, True), ('phone', 80, True),
            ('address', 400, True), ('hours', 300, True)]}
        values.update({k: str(db.parse_money(form.get(k))) for k in ('delivery_fee', 'free_delivery_from', 'minimum_order')})
        values.update({k: '1' if form.get(k) == 'on' else '0' for k in ('orders_open', 'delivery_enabled', 'auto_accept')})
        for k in ('card_on_receipt','delivery_districts'):
            values[k] = '1' if form.get(k) == 'on' else '0'
        percent = int(form.get('pickup_discount') or 0)
        if not 0 <= percent <= 50:
            raise ValueError('Скидка должна быть от 0 до 50%')
        values['pickup_discount'] = str(percent)
        values['delivery_time'] = field(form,'delivery_time',100,False)
        with db.connect(True) as c:
            c.executemany('UPDATE settings SET value=? WHERE key=?', [(v, k) for k, v in values.items()])
        return redirect('/admin/settings', ok='Настройки сохранены')
    except ValueError as exc:
        return redirect('/admin/settings', error=exc)


@app.get('/admin/notifications')
def notification_page(request: Request):
    require_admin(request)
    from .order_updates import telegram_state
    with db.connect() as c:
        rows = c.execute('SELECT * FROM outbox ORDER BY id DESC LIMIT 100').fetchall()
        customer_rows=c.execute('''SELECT m.*,a.platform,a.chat_id,a.notifications FROM customer_messages m
            JOIN customer_accounts a ON a.id=m.account_id ORDER BY m.id DESC LIMIT 100''').fetchall()
        max_rows=c.execute('''SELECT m.*,r.chat_id,r.notifications FROM max_order_messages m
            JOIN max_order_recipients r ON r.order_id=m.order_id ORDER BY m.id DESC LIMIT 100''').fetchall()
        missing = c.execute("SELECT count(*) FROM orders WHERE notified=0 AND status NOT IN ('cancelled','done') AND (payment_method<>'tbank' OR payment_status='paid')").fetchone()[0]
    return render(request, 'notifications.html', notifications=rows, customer_notifications=customer_rows, max_notifications=max_rows,
                  telegram=telegram_state(), missing=missing, page='notifications')


@app.post('/admin/customer-notifications/{nid:int}/retry')
async def retry_customer_notification(request: Request, nid: int):
    await form_data(request)
    with db.connect(True) as c:
        c.execute("UPDATE customer_messages SET sent=0,attempts=0,next_try=0,error='' WHERE id=? AND sent=-1",(nid,))
    return redirect('/admin/notifications',ok='Повтор сообщения покупателю запланирован')


@app.post('/admin/max-notifications/{nid:int}/retry')
async def retry_max_notification(request: Request, nid: int):
    await form_data(request)
    with db.connect(True) as c:
        c.execute("UPDATE max_order_messages SET sent=0,attempts=0,next_try=0,error='' WHERE id=? AND sent=-1",(nid,))
    return redirect('/admin/notifications',ok='Повтор сообщения MAX запланирован')


@app.post('/admin/notifications/{nid:int}/retry')
async def retry_notification(request: Request, nid: int):
    await form_data(request)
    with db.connect(True) as c:
        c.execute('UPDATE outbox SET sent=0,attempts=0,next_try=0 WHERE id=? AND sent=-1', (nid,))
    return redirect('/admin/notifications', ok='Повторная отправка запланирована')


@app.get('/admin/media/{filename}')
def media(request: Request, filename: str):
    require_admin(request)
    if Path(filename).name != filename or not filename.endswith('.jpg'):
        raise HTTPException(404)
    path = config.MEDIA / filename
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type='image/jpeg')


@app.get('/login')
def legacy_login():
    return redirect('/admin/login')


from .store import router as store_router
from .content_admin import router as content_router
from .menu_import import router as menu_router
from .menu_sync import router as sync_router
from .max_bot import router as max_router
app.include_router(store_router)
app.include_router(content_router)
app.include_router(menu_router)
app.include_router(sync_router)
app.include_router(max_router)
from .order_updates import router as updates_router
app.include_router(updates_router)
from .reporting import router as reporting_router
app.include_router(reporting_router)
