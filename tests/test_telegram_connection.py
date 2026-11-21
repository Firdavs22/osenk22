import asyncio

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetMe

from app import config, telegram_connection as connection
from app.vault import set_config
from test_admin import login


TOKEN = '123456:TEST_TOKEN_NOT_USED_FOR_NETWORK'


@pytest.fixture(autouse=True)
def clean_proxy(monkeypatch):
    monkeypatch.setattr(config, 'TELEGRAM_PROXY_URL', '')
    monkeypatch.setattr(config, 'TELEGRAM_PROXY_USERNAME', '')
    monkeypatch.setattr(config, 'TELEGRAM_PROXY_PASSWORD', '')


def test_socks5_auth_and_remote_dns(monkeypatch):
    received = []

    async def run():
        finished = asyncio.get_running_loop().create_future()

        async def handle(reader, writer):
            try:
                version, size = await reader.readexactly(2)
                methods = await reader.readexactly(size)
                assert version == 5 and 2 in methods
                writer.write(b'\x05\x02')
                await writer.drain()
                version, size = await reader.readexactly(2)
                username = await reader.readexactly(size)
                size = (await reader.readexactly(1))[0]
                password = await reader.readexactly(size)
                assert version == 1
                received.append((username, password))
                writer.write(b'\x01\x00')
                await writer.drain()
                assert await reader.readexactly(4) == b'\x05\x01\x00\x03'
                size = (await reader.readexactly(1))[0]
                host = await reader.readexactly(size)
                port = int.from_bytes(await reader.readexactly(2), 'big')
                received.append((host, port))
                # Refuse the tunnel after verifying routing. No Telegram traffic leaves the test.
                writer.write(b'\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00')
                await writer.drain()
                finished.set_result(None)
            except Exception as exc:
                finished.set_exception(exc)
            finally:
                writer.close()
                await writer.wait_closed()

        async with await asyncio.start_server(handle, '127.0.0.1', 0) as server:
            port = server.sockets[0].getsockname()[1]
            monkeypatch.setattr(config, 'TELEGRAM_PROXY_URL', f'socks5://127.0.0.1:{port}')
            monkeypatch.setattr(config, 'TELEGRAM_PROXY_USERNAME', 'shop-user')
            monkeypatch.setattr(config, 'TELEGRAM_PROXY_PASSWORD', 'secret:@/#$')
            async with connection.create_bot(TOKEN, timeout=3) as bot:
                with pytest.raises(TelegramNetworkError) as error:
                    await bot.get_me()
            await asyncio.wait_for(finished, 3)
            message = connection.connection_error(error.value)
            assert 'прокси' in message and 'secret' not in message and TOKEN not in message

    asyncio.run(run())
    assert received == [(b'shop-user', b'secret:@/#$'), (b'api.telegram.org', 443)]


@pytest.mark.parametrize('url', ['tg://proxy?secret=PRIVATE', 'socks5://user:PRIVATE@host:invalid',
                                 'socks5://host', 'socks5://host:0', 'socks5://host:1080/path'])
def test_invalid_proxy_does_not_fall_back_or_expose_credentials(monkeypatch, url):
    monkeypatch.setattr(config, 'TELEGRAM_PROXY_URL', url)
    with pytest.raises(connection.ProxyConfigurationError) as error:
        connection.create_bot(TOKEN)
    assert 'PRIVATE' not in str(error.value)


def test_direct_connection_and_cli_check(shop, monkeypatch, capsys):
    set_config('telegram', {'enabled': True, 'token': TOKEN})
    calls = []

    async def fake_request(self, bot, method, timeout=None):
        from aiogram.types import User
        assert self.proxy is None
        calls.append(method.__api_method__)
        return User(id=123456, is_bot=True, first_name='Shop', username='test_sushi_bot')

    monkeypatch.setattr(connection.AiohttpSession, 'make_request', fake_request)
    assert asyncio.run(connection.main()) == 0
    assert calls == ['getMe']
    assert 'успешно' in capsys.readouterr().out


def test_admin_check_uses_proxy_and_redacts_transport_errors(client, monkeypatch):
    set_config('telegram', {'enabled': True, 'token': TOKEN})
    monkeypatch.setattr(config, 'TELEGRAM_PROXY_URL', 'socks5://127.0.0.1:1080')

    async def failed_request(self, bot, method, timeout=None):
        assert self.proxy == config.TELEGRAM_PROXY_URL
        raise TelegramNetworkError(method=GetMe(), message='PRIVATE ' + TOKEN)

    monkeypatch.setattr(connection.AiohttpSession, 'make_request', failed_request)
    csrf = login(client)
    response = client.post('/admin/integrations/telegram/check', data={'csrf': csrf})
    assert response.status_code == 200
    assert 'соединение через прокси не удалось' in response.text
    assert 'PRIVATE' not in response.text and TOKEN not in response.text
