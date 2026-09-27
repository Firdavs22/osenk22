"""Read-only MAX connection check. Does not send messages or register webhooks."""
import asyncio

from . import config
from .max_bot import api, connection_error
from .vault import get_config


async def main():
    cfg=get_config('max')
    print('MAX_CA_BUNDLE:', 'задан' if config.MAX_CA_BUNDLE else 'не задан', flush=True)
    if not cfg.get('token'):
        print('Токен MAX не сохранён в админке.')
        return
    try:
        await api(cfg,'GET','/me')
        print('TLS и токен: успешно. Сообщения не отправлялись.')
    except Exception as exc:
        print(connection_error(exc))


if __name__=='__main__':
    asyncio.run(main())
