import asyncio
from urllib.parse import urlsplit

from aiohttp import BasicAuth
from aiohttp_socks import ProxyConnectionError, ProxyError, ProxyTimeoutError
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.utils.token import TokenValidationError, validate_token

from . import config


class ProxyConfigurationError(ValueError):
    pass


class TelegramSession(AiohttpSession):
    async def make_request(self, bot, method, timeout=None):
        try:
            return await super().make_request(bot, method, timeout=timeout)
        except (ProxyConnectionError, ProxyError, ProxyTimeoutError, TelegramNetworkError):
            # Keep proxy failures in aiogram's retry path without logging credentials.
            raise TelegramNetworkError(method=method, message='Ошибка соединения с Telegram.') from None


def create_bot(token, *, timeout=60):
    validate_token(token)
    proxy = config.TELEGRAM_PROXY_URL
    if not proxy:
        return Bot(token, session=TelegramSession(timeout=timeout))
    try:
        address = urlsplit(proxy)
        if (address.scheme not in ('socks5', 'socks4', 'http')
                or not address.hostname or not address.port
                or address.path not in ('', '/') or address.query or address.fragment):
            raise ValueError
        if config.TELEGRAM_PROXY_USERNAME:
            proxy = (proxy, BasicAuth(config.TELEGRAM_PROXY_USERNAME,
                                      config.TELEGRAM_PROXY_PASSWORD))
        elif config.TELEGRAM_PROXY_PASSWORD:
            raise ValueError
        session = TelegramSession(proxy=proxy, timeout=timeout)
    except (ValueError, TypeError):
        raise ProxyConfigurationError(
            'Проверьте TELEGRAM_PROXY_URL (socks5://адрес:порт) и логин прокси в .env.'
        ) from None
    except RuntimeError:
        raise ProxyConfigurationError(
            'Установите зависимости прокси: .venv/bin/python -m pip install -r requirements.lock.txt'
        ) from None
    return Bot(token, session=session)


def connection_error(exc):
    # Transport exceptions may contain proxy credentials or the bot token.
    if isinstance(exc, ProxyConfigurationError):
        return str(exc)
    if isinstance(exc, (TokenValidationError, TelegramUnauthorizedError)):
        return 'Telegram: проверьте сохранённый токен бота.'
    if isinstance(exc, (TelegramNetworkError, OSError, TimeoutError)):
        if config.TELEGRAM_PROXY_URL:
            return 'Telegram: соединение через прокси не удалось. Проверьте адрес, порт, логин, пароль и доступ прокси к Telegram.'
        return 'Telegram: соединение не удалось. Проверьте доступ VPS к api.telegram.org или настройте TELEGRAM_PROXY_URL.'
    return 'Telegram: проверка соединения не выполнена. Проверьте настройки и повторите попытку.'


async def main():
    from .vault import get_config

    print('Прокси Telegram:', 'задан' if config.TELEGRAM_PROXY_URL else 'не задан', flush=True)
    try:
        cfg = get_config('telegram')
        if not cfg.get('enabled', True):
            print('Telegram выключен в настройках сайта.')
            return 1
        async with create_bot(cfg.get('token') or config.BOT_TOKEN, timeout=15) as bot:
            me = await bot.get_me()
        print(f'Соединение и токен: успешно. Бот @{me.username}.')
        return 0
    except Exception as exc:
        print(connection_error(exc))
        return 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
