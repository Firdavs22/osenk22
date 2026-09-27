import asyncio
import logging
import re
import secrets
import time
from contextlib import suppress

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter, TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (BotCommand, CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo, MenuButtonWebApp, MenuButtonCommands)

from . import config, db
from .chat_screen import screen
from .mini_apps import app_url

log = logging.getLogger(__name__)
dp = Dispatcher()


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=action) for label, action in row]
        for row in rows])


def home_keyboard():
    markup = keyboard([[('🍣 Меню в чате', 'menu'), ('🛒 Корзина чата', 'cart')],
                       [('📦 Заказы в чате', 'orders'), ('📍 О магазине', 'info')]])
    url = app_url('telegram')
    if url:
        markup.inline_keyboard.insert(0,[InlineKeyboardButton(text='🍣 Открыть меню с фото',web_app=WebAppInfo(url=url))])
    return markup


async def send(message, text, reply_markup=None):
    await screen(message, text, reply_markup)


async def show_menu(message):
    available = {p['category_id'] for p in db.products(active=True)}
    rows = [[(c['name'], f'cat:{c["id"]}:0')] for c in db.categories() if c['id'] in available]
    rows.append([('🛒 Корзина', 'cart')])
    markup = keyboard(rows)
    url = app_url('telegram')
    if url:
        markup.inline_keyboard.insert(0,[InlineKeyboardButton(text='🍣 Открыть меню с фото',web_app=WebAppInfo(url=url))])
    await screen(message,'🍣 Выберите категорию' + (' или откройте мини-приложение. Корзина мини-приложения ведётся отдельно от корзины чата.' if url else ''),reply_markup=markup)


async def show_cart(message, user):
    items = db.cart(user)
    if not items:
        await screen(message, 'Ваша корзина пока пуста.', reply_markup=home_keyboard())
        return
    text = '🛒 Ваша корзина\n\n' + '\n'.join(
        f'{p["name"]} × {p["quantity"]} — {db.money(p["price"] * p["quantity"])}' +
        (' · НЕТ В НАЛИЧИИ' if not p['active'] else '') for p in items)
    text += '\n\nТовары: ' + db.money(sum(p['price'] * p['quantity'] for p in items))
    text += '\nСтоимость доставки рассчитаем перед подтверждением.'
    rows = [[(f'− {p["name"][:22]}', f'minus:{p["id"]}'),
             (f'+ ({p["quantity"]})', f'plus:{p["id"]}'), ('✕', f'remove:{p["id"]}')] for p in items]
    rows += [[('✅ Оформить заказ', 'checkout')], [('Меню', 'menu'), ('Очистить', 'clear')]]
    await send(message, text, keyboard(rows))


async def show_orders(message, user):
    with db.connect() as c:
        orders = c.execute('SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 5', (user,)).fetchall()
        details = [(o, c.execute('SELECT * FROM order_items WHERE order_id=?', (o['id'],)).fetchall()) for o in orders]
    if not orders:
        await screen(message, 'У вас ещё нет заказов.', reply_markup=home_keyboard())
    else:
        await send(message, '\n\n────────\n\n'.join(db.order_summary(order, items) for order, items in details), home_keyboard())


async def show_info(message):
    s = db.settings()
    text = (f'{s["shop_name"]}\n\n📍 {s["address"] or "Адрес уточняется"}'
            f'\n☎ {s["phone"] or "Телефон уточняется"}\n🕒 {s["hours"]}'
            f'\n\nМинимальная сумма товаров: {db.money(s["minimum_order"])}'
            f'\nДоставка: {db.money(s["delivery_fee"])}')
    if int(s['free_delivery_from']):
        text += f'\nБесплатная доставка от {db.money(s["free_delivery_from"])}'
    text += '\nДоступно: самовывоз' + (' и доставка' if s['delivery_enabled'] == '1' else '')
    text += '\nОплата при получении. Зону и время доставки подтвердит магазин.'
    text += '\nПриём заказов: ' + ('открыт' if s['orders_open'] == '1' else 'закрыт')
    await send(message, text, home_keyboard())


@dp.message(Command('start', 'menu', 'cancel', 'id', 'orders', 'privacy', 'stopupdates'))
async def commands(message: Message):
    if message.chat.type != 'private':
        return
    user = message.from_user.id
    command = message.text.split()[0].split('@')[0]
    if command == '/stopupdates':
        from .order_updates import unsubscribe_chat
        unsubscribe_chat(message.chat.id,message.bot.id)
        await screen(message,'Уведомления о заказах с сайта отключены. Для новой подписки откройте страницу нужного заказа.',home_keyboard())
        return
    if command == '/start' and len(message.text.split())==2 and message.text.split()[1].startswith('watch_'):
        from .order_updates import subscribe
        try:
            text = subscribe(message.text.split()[1][6:],message.chat.id,message.bot.id)
        except ValueError as exc:
            text = str(exc)
        await screen(message,text,home_keyboard())
        return
    if command == '/id':
        await screen(message, f'Ваш Telegram ID: {user}')
        return
    if command == '/orders':
        await show_orders(message, user)
        return
    if command == '/privacy':
        await screen(message, 'Для обработки заказа магазин получает ваш Telegram ID, имя, телефон, '
                             'адрес, комментарий и состав заказа. Они сохраняются на сервере магазина. '
                             'По вопросам использования и удаления данных обратитесь в магазин: ' +
                             (db.settings()['phone'] or 'контакт указан в разделе «О магазине».'))
        return
    db.save_draft(user, None, {})
    await screen(message, 'Оформление отменено. Корзина сохранена.' if command == '/cancel' else
                         f'Добро пожаловать в {db.settings()["shop_name"]}! 🍣\nВыберите блюда — мы приготовим ваш заказ.',
                         reply_markup=ReplyKeyboardRemove())
    await show_menu(message)


@dp.callback_query()
async def callbacks(call: CallbackQuery):
    if not call.message or call.message.chat.type != 'private':
        await call.answer()
        return
    await call.answer()
    user, message = call.from_user.id, call.message
    action = call.data or ''
    try:
        if action == 'menu':
            db.save_draft(user, None, {})
            await show_menu(message)
        elif action.startswith('cat:'):
            _, cat, page = action.split(':')
            page = max(0, int(page))
            items = db.products(int(cat), active=True)
            rows = [[(f'{p["name"]} · {db.money(p["price"])}', f'product:{p["id"]}')] for p in items[page*8:page*8+8]]
            nav = []
            if page > 0:
                nav.append(('← Назад', f'cat:{cat}:{page-1}'))
            if (page+1)*8 < len(items):
                nav.append(('Ещё →', f'cat:{cat}:{page+1}'))
            if nav:
                rows.append(nav)
            rows.append([('Категории', 'menu'), ('Корзина', 'cart')])
            await screen(message, 'Выберите блюдо' if items else 'В этой категории пока нет доступных блюд.', reply_markup=keyboard(rows))
        elif action.startswith('product:'):
            p = db.product(int(action.split(':')[1]))
            if not p or not p['active']:
                raise ValueError('Товар сейчас недоступен')
            text = f'{p["name"]}\n{db.money(p["price"])} · {p["weight"]}\n\nСостав: {p["ingredients"]}'
            if p['description']:
                text += '\n\n' + p['description']
            markup = keyboard([[('➕ В корзину', f'add:{p["id"]}')], [('Меню', 'menu'), ('Корзина', 'cart')]])
            photo = config.MEDIA / p['photo'] if p['photo'] else None
            await screen(message,text,markup,photo=photo if photo and photo.is_file() else None)
        elif action.split(':')[0] in ('add', 'plus', 'minus', 'remove'):
            kind, pid = action.split(':')
            delta = -99 if kind == 'remove' else (-1 if kind == 'minus' else 1)
            db.cart_change(user, int(pid), delta)
            if kind == 'add':
                await screen(message, 'Добавлено в корзину ✓', reply_markup=home_keyboard())
            else:
                await show_cart(message, user)
        elif action == 'cart':
            db.save_draft(user, None, {})
            await show_cart(message, user)
        elif action == 'clear':
            db.clear_cart(user)
            await show_cart(message, user)
        elif action == 'info':
            await show_info(message)
        elif action == 'orders':
            await show_orders(message, user)
        elif action == 'checkout':
            db.quote(user, {'method': 'pickup'})
            db.save_draft(user, 'method', {})
            rows = [[('Самовывоз', 'method:pickup')]]
            if db.settings()['delivery_enabled'] == '1':
                rows.insert(0, [('Доставка', 'method:delivery')])
            rows.append([('Отмена', 'cancel')])
            await screen(message, 'Как вам удобно получить заказ?', reply_markup=keyboard(rows))
        elif action.startswith('method:'):
            step, data = db.draft(user)
            if step != 'method':
                raise ValueError('Начните оформление через корзину')
            data['method'] = action.split(':')[1]
            if data['method'] == 'delivery' and db.settings().get('delivery_districts') == '1':
                from .shop_policy import DISTRICTS
                db.save_draft(user, 'district', data)
                await screen(message, 'Выберите район. Для доставки за пределы этих районов заранее согласуйте стоимость по телефону ' + db.settings()['phone'],
                    reply_markup=keyboard([[(name, 'district:'+str(i))] for i,name in enumerate(DISTRICTS)] + [[('Отмена','cancel')]]))
                return
            db.quote(user, data)
            db.save_draft(user, 'customer', data)
            await screen(message, 'Как к вам обращаться? Напишите имя.\nДля отмены — /cancel', reply_markup=ReplyKeyboardRemove())
        elif action.startswith('district:'):
            from .shop_policy import DISTRICTS
            step, data = db.draft(user)
            if step != 'district':
                raise ValueError('Начните оформление через корзину')
            index = int(action.split(':')[1])
            if not 0 <= index < len(DISTRICTS):
                raise ValueError('Выберите район кнопкой')
            data['district'] = DISTRICTS[index]
            db.quote(user,data)
            db.save_draft(user,'customer',data)
            await screen(message, 'Как к вам обращаться? Напишите имя.',reply_markup=ReplyKeyboardRemove())
        elif action == 'skip':
            step, data = db.draft(user)
            if step != 'comment':
                raise ValueError('Эта кнопка устарела')
            data['comment'] = ''
            await confirm_preview(message, user, data)
        elif action.startswith('confirm:'):
            oid = db.place_order(user, action.split(':')[1])
            await screen(message, f'Заказ №{oid} сохранён. Подробности придут отдельным сообщением.', reply_markup=home_keyboard())
        elif action == 'cancel':
            db.save_draft(user, None, {})
            await screen(message, 'Оформление отменено. Корзина сохранена.', reply_markup=ReplyKeyboardRemove())
            await show_menu(message)
    except (ValueError, IndexError) as exc:
        await screen(message, str(exc) or 'Не удалось выполнить действие', reply_markup=home_keyboard())


async def ask_comment(message, user, data):
    db.save_draft(user, 'comment', data)
    await screen(message, 'Комментарий к заказу: количество приборов, пожелания, аллергии.\n'
                         'Напишите текст или нажмите «Без комментария».',
                         reply_markup=keyboard([[('Без комментария', 'skip')], [('Отмена', 'cancel')]]))


async def confirm_preview(message, user, data):
    q = db.quote(user, data)
    data.update(token=secrets.token_hex(12), fingerprint=q['fingerprint'])
    db.save_draft(user, 'confirm', data)
    summary = dict(data, id='на подтверждении', status='new', discount=q['discount'], delivery=q['delivery'], total=q['total'], currency=q['currency'])
    text = db.order_summary(summary, q['items'])
    text += '\n\nПроверьте данные. Подтверждая заказ, вы передаёте магазину указанные контакты для его выполнения. /privacy'
    await send(message, text, keyboard([[('✅ Подтвердить заказ', f'confirm:{data["token"]}')], [('Отмена', 'cancel')]]))


@dp.message(F.chat.type == 'private')
async def checkout_text(message: Message):
    user = message.from_user.id
    step, data = db.draft(user)
    text = (message.text or '').strip()
    try:
        if step == 'customer':
            if not 2 <= len(text) <= 80:
                raise ValueError('Введите имя от 2 до 80 символов')
            data['customer'] = text
            db.save_draft(user, 'phone', data)
            await screen(message, 'Укажите телефон или отправьте свой контакт кнопкой ниже.',
                reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text='📱 Отправить мой телефон', request_contact=True)]], resize_keyboard=True, one_time_keyboard=True))
        elif step == 'phone':
            if message.contact:
                if message.contact.user_id != user:
                    raise ValueError('Отправьте свой контакт или введите номер вручную')
                text = message.contact.phone_number
            phone = re.sub(r'[\s()\-]', '', text)
            if not re.fullmatch(r'\+?[0-9]{10,15}', phone):
                raise ValueError('Введите телефон: от 10 до 15 цифр, можно с + в начале')
            data['phone'] = phone
            if data['method'] == 'delivery':
                db.save_draft(user, 'address', data)
                await screen(message, 'Напишите адрес: город, улица, дом, квартира, подъезд, этаж.', reply_markup=ReplyKeyboardRemove())
            else:
                data['address'] = db.settings()['address'] or 'Адрес самовывоза уточнит магазин'
                await screen(message, 'Вы выбрали самовывоз.', reply_markup=ReplyKeyboardRemove())
                await ask_comment(message, user, data)
        elif step == 'address':
            if not 10 <= len(text) <= 400:
                raise ValueError('Введите полный адрес, от 10 до 400 символов')
            data['address'] = text
            await ask_comment(message, user, data)
        elif step == 'comment':
            if not text or len(text) > 500:
                raise ValueError('Комментарий должен содержать от 1 до 500 символов')
            data['comment'] = text
            await confirm_preview(message, user, data)
        elif step == 'confirm':
            await confirm_preview(message, user, data)
        elif step == 'district':
            from .shop_policy import DISTRICTS
            await screen(message, 'Выберите район доставки.', reply_markup=keyboard(
                [[(name, 'district:'+str(i))] for i,name in enumerate(DISTRICTS)] + [[('Отмена','cancel')]]))
        elif step == 'method':
            rows = [[('Самовывоз', 'method:pickup')], [('Отмена', 'cancel')]]
            if db.settings()['delivery_enabled']=='1':
                rows.insert(0,[('Доставка', 'method:delivery')])
            await screen(message, 'Выберите доставку или самовывоз.', reply_markup=keyboard(rows))
        else:
            await screen(message, 'Откройте меню, чтобы выбрать блюда.', reply_markup=home_keyboard())
    except ValueError as exc:
        markup = keyboard([[('Без комментария','skip')], [('Отмена','cancel')]]) if step=='comment' else None
        await screen(message, str(exc), reply_markup=markup)


async def deliver_once(bot):
    from .order_updates import token_fingerprint
    with db.connect(True) as c:
        c.execute('UPDATE telegram_runtime SET heartbeat=? WHERE id=1 AND fingerprint=?',(time.time(),token_fingerprint(bot.token)))
        from .store import notify_order
        missing = c.execute("SELECT id FROM orders WHERE notified=0 AND status NOT IN ('cancelled','done') AND (payment_method<>'tbank' OR payment_status='paid') ORDER BY id LIMIT 50").fetchall()
        for order in missing:
            notify_order(c,order['id'])
    with db.connect() as c:
        rows = c.execute('SELECT * FROM outbox WHERE sent=0 AND next_try<=? ORDER BY id LIMIT 20', (time.time(),)).fetchall()
    for row in rows:
        if row['subscription_order']:
            with db.connect() as c:
                valid = c.execute('SELECT 1 FROM order_subscriptions WHERE order_id=? AND chat_id=? AND bot_id=?',
                                  (row['subscription_order'],row['chat_id'],bot.id)).fetchone()
            if not valid or row['subscription_bot']!=bot.id:
                with db.connect(True) as c:
                    c.execute("UPDATE outbox SET sent=-1,error='Подписка отключена или изменился бот' WHERE id=?",(row['id'],))
                continue
        state, delay = 0, min(3600, 2 ** min(row['attempts'] + 2, 12))
        error = ''
        try:
            await bot.send_message(row['chat_id'], row['text'])
            state = 1
        except TelegramRetryAfter as exc:
            delay = exc.retry_after + 1
            error = 'Telegram ограничил частоту. Повторим автоматически.'
        except TelegramForbiddenError:
            state = -1
            error = 'Бот заблокирован или нет доступа к чату. Получателю нужно открыть бота и нажать /start.'
            log.warning('Notification %s blocked by recipient', row['id'])
        except TelegramBadRequest:
            state = -1
            error = 'Telegram отклонил сообщение. Проверьте ID получателя и что он отправил этому боту /start.'
        except TelegramAPIError:
            error = 'Ошибка связи с Telegram. Повторим автоматически; проверьте доступ сервера к api.telegram.org.'
            if row['attempts'] >= 11:
                state = -1
                error = 'Попытки отправки исчерпаны. Проверьте подключение Telegram и нажмите «Повторить».'
            log.warning('Notification %s failed; attempt %s', row['id'], row['attempts'] + 1)
        with db.connect(True) as c:
            c.execute('UPDATE outbox SET sent=?,attempts=attempts+1,next_try=?,error=?,last_attempt=? WHERE id=?',
                      (state, time.time() + delay,error,time.time(),row['id']))


async def notification_loop(bot):
    while True:
        try:
            await deliver_once(bot)
        except Exception:
            log.exception('Notification worker error')
        await asyncio.sleep(2)


def telegram_token():
    from .vault import get_config
    cfg = get_config('telegram')
    return (cfg.get('token') or config.BOT_TOKEN) if cfg.get('enabled', True) else ''


async def watch_token(token):
    initial_url = app_url('telegram')
    while telegram_token() == token and app_url('telegram') == initial_url:
        await asyncio.sleep(15)


async def main():
    db.init()
    while True:
        token = telegram_token()
        if not token:
            await asyncio.sleep(15)
            continue
        bot = Bot(token)
        tasks = []
        try:
            from .order_updates import record_bot
            me = await bot.get_me()
            record_bot(me.id,me.username,token,heartbeat=True)
            await bot.delete_webhook(drop_pending_updates=False)
            url = app_url('telegram')
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text='Меню',web_app=WebAppInfo(url=url)) if url else MenuButtonCommands())
            await bot.set_my_commands([BotCommand(command='menu', description='Меню'),
                                       BotCommand(command='orders', description='Мои заказы'),
                                       BotCommand(command='cancel', description='Отменить оформление'),
                                       BotCommand(command='privacy', description='Обработка данных'),
                                       BotCommand(command='id', description='Мой Telegram ID')])
            tasks = [asyncio.create_task(notification_loop(bot)),
                     asyncio.create_task(dp.start_polling(bot, handle_as_tasks=False, close_bot_session=False, handle_signals=False)),
                     asyncio.create_task(watch_token(token))]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except TelegramAPIError:
            log.warning('Telegram unavailable; retrying in 15 seconds')
            await asyncio.sleep(15)
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task
            await bot.session.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
