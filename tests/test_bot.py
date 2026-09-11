import asyncio
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import Message, Update, User

from app import db
from app.bot import dp, deliver_once


class FakeTelegram(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        yield b''

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if method.__api_method__ == 'answerCallbackQuery':
            return True
        if method.__api_method__ == 'getMe':
            return User(id=123456, is_bot=True, first_name='Sushi', username='sushi_test_bot')
        return Message(message_id=len(self.calls), date=datetime.now(timezone.utc),
                       chat={'id': int(getattr(method, 'chat_id', 100)), 'type': 'private'},
                       text=getattr(method, 'text', ''))


def test_full_telegram_delivery_and_duplicate_confirmation(shop):
    async def run():
        session = FakeTelegram()
        bot = Bot('123456:TEST_TOKEN_NOT_USED_FOR_NETWORK', session=session)
        seq = 0

        async def message(text):
            nonlocal seq
            seq += 1
            payload = {'update_id': seq, 'message': {'message_id': seq, 'date': 1750000000,
                'from': {'id': 100, 'is_bot': False, 'first_name': 'Иван'},
                'chat': {'id': 100, 'type': 'private'}, 'text': text}}
            if text.startswith('/'):
                payload['message']['entities'] = [{'type': 'bot_command', 'offset': 0, 'length': len(text)}]
            await dp.feed_update(bot, Update.model_validate(payload))

        async def callback(data):
            nonlocal seq
            seq += 1
            await dp.feed_update(bot, Update.model_validate({'update_id': seq, 'callback_query': {
                'id': str(seq), 'chat_instance': 'test', 'from': {'id': 100, 'is_bot': False, 'first_name': 'Иван'},
                'data': data, 'message': {'message_id': seq, 'date': 1750000000,
                    'chat': {'id': 100, 'type': 'private'}, 'text': 'Кнопки'}}}))

        await message('/start')
        await callback('cat:1:0')
        await callback('product:1')
        await callback('add:1')
        await callback('cart')
        await callback('checkout')
        await callback('method:delivery')
        await message('Иван')
        await message('не телефон')
        assert db.draft(100)[0] == 'phone'
        await message('+7 (999) 123-45-67')
        await message('Москва, Тестовая улица, дом 1, квартира 2')
        await callback('skip')
        step, data = db.draft(100)
        assert step == 'confirm'
        await callback('confirm:' + data['token'])
        await callback('confirm:' + data['token'])
        await callback('orders')
        await deliver_once(bot)
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM orders').fetchone()[0] == 1
            assert c.execute('SELECT count(*) FROM outbox WHERE sent=1').fetchone()[0] == 2
            assert c.execute('SELECT phone FROM orders').fetchone()[0] == '+79991234567'
        texts = [getattr(m, 'text', '') or '' for m in session.calls]
        assert any('Филадельфия' in t for t in texts)
        assert any('Новый заказ' in t for t in texts)
        assert any('Спасибо!' in t for t in texts)
        await bot.session.close()
    asyncio.run(run())


def test_outbox_handles_blocked_and_rate_limited_recipients(shop):
    class FailingTelegram(FakeTelegram):
        async def make_request(self, bot, method, timeout=None):
            if method.chat_id == 1:
                raise TelegramForbiddenError(method=method, message='blocked')
            raise TelegramRetryAfter(method=method, message='slow down', retry_after=30)

    with db.connect(True) as c:
        db.enqueue(c, 1, 'Blocked user')
        db.enqueue(c, 2, 'Retry later')

    async def run():
        bot = Bot('123456:TEST_TOKEN_NOT_USED_FOR_NETWORK', session=FailingTelegram())
        await deliver_once(bot)
        await bot.session.close()
    asyncio.run(run())
    with db.connect() as c:
        rows = c.execute('SELECT * FROM outbox ORDER BY id').fetchall()
        assert rows[0]['sent'] == -1
        assert rows[1]['sent'] == 0
        assert rows[1]['attempts'] == 1
        assert rows[1]['next_try'] > 0
