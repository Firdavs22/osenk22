"""Replace only navigation messages created by this bot; never touch user/order messages."""
import asyncio
import json
from contextlib import suppress
from weakref import WeakValueDictionary

from aiogram.exceptions import TelegramBadRequest, TelegramAPIError
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InputMediaPhoto

from . import db

locks = WeakValueDictionary()


async def screen(message, text, reply_markup=None, photo=None):
    bot, chat = message.bot, message.chat.id
    key = (bot.id, chat)
    lock = locks.setdefault(key, asyncio.Lock())
    async with lock:
        with db.connect() as c:
            row = c.execute('SELECT messages FROM telegram_screens WHERE bot_id=? AND chat_id=?',key).fetchone()
        previous = json.loads(row['messages']) if row else []
        inline = reply_markup is None or isinstance(reply_markup, InlineKeyboardMarkup)
        one = len(text)<= (1000 if photo else 3500)
        if inline and one and len(previous)==1:
            old = previous[0]
            try:
                if photo and old['kind']=='photo':
                    await bot.edit_message_media(chat_id=chat,message_id=old['id'],
                        media=InputMediaPhoto(media=FSInputFile(photo),caption=text),reply_markup=reply_markup)
                    return
                if not photo and old['kind']=='text':
                    await bot.edit_message_text(chat_id=chat,message_id=old['id'],text=text,reply_markup=reply_markup)
                    return
            except TelegramBadRequest as exc:
                if 'message is not modified' in exc.message.lower():
                    return
                # Deleted/old/incompatible message: create a replacement below.
        created = []
        try:
            if photo:
                try:
                    sent = await message.answer_photo(FSInputFile(photo),caption=text if one else None,
                                                      reply_markup=reply_markup if one else None)
                    created.append({'id':sent.message_id,'kind':'photo'})
                except TelegramBadRequest:
                    photo = None
            if not photo or not one:
                for offset in range(0,len(text),3500):
                    sent = await message.answer(text[offset:offset+3500],
                        reply_markup=reply_markup if offset+3500>=len(text) else None)
                    created.append({'id':sent.message_id,'kind':'text'})
        except BaseException:
            for item in created:
                with suppress(TelegramAPIError):
                    await bot.delete_message(chat_id=chat,message_id=item['id'])
            raise
        with db.connect(True) as c:
            c.execute('INSERT INTO telegram_screens VALUES (?,?,?) ON CONFLICT(bot_id,chat_id) DO UPDATE SET messages=excluded.messages',
                      (*key,json.dumps(created)))
        for old in previous:
            with suppress(TelegramAPIError):
                await bot.delete_message(chat_id=chat,message_id=old['id'])
