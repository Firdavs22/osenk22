"""Shared guest storefront. Messenger launch parameters are NOT authentication."""
from urllib.parse import urlsplit
import base64
import json

from itsdangerous import BadSignature, TimestampSigner
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import HTTPConnection

from . import config
from .vault import get_config


def public_origin(value):
    value = str(value or '').strip().rstrip('/')
    if not value:
        return ''
    try:
        url = urlsplit(value)
        if (url.scheme!='https' or not url.hostname or '.' not in url.hostname or url.username or url.password
                or url.port not in (None,443) or url.path or url.query or url.fragment):
            raise ValueError()
    except ValueError:
        raise ValueError('Адрес магазина: https://ваш-домен, без пути, пароля и параметров.') from None
    return value


def app_url(platform):
    cfg = get_config(platform)
    try:
        origin = public_origin(cfg.get('public_url') or config.PUBLIC_URL)
    except ValueError:
        return ''
    if platform=='telegram' and not cfg.get('mini_app',False):
        return ''
    return origin+'/mini/'+platform if origin else ''


class ShopSessions:
    """Keep admin Strict cookies separate from the embedded guest cart cookie."""
    def __init__(self, app):
        self.admin = SessionMiddleware(app,secret_key=config.SESSION_SECRET,session_cookie='sushi_admin',
            max_age=8*3600,same_site='strict',https_only=config.COOKIE_SECURE)
        async def public_app(scope, receive, send):
            if scope['type'] in ('http','websocket') and not scope.get('session'):
                cookies = HTTPConnection(scope).cookies
                legacy = cookies.get('sushi_admin') if 'sushi_store' not in cookies else None
                if legacy:
                    try:
                        old = json.loads(base64.b64decode(TimestampSigner(config.SESSION_SECRET).unsign(legacy,max_age=8*3600)))
                        if type(old.get('visitor')) is int and old['visitor']<0:
                            scope['session'].update({k:old[k] for k in ('visitor','csrf','checkout_key') if k in old})
                    except (BadSignature,ValueError,TypeError,AttributeError):
                        pass
            await app(scope,receive,send)
        self.public = SessionMiddleware(public_app,secret_key=config.SESSION_SECRET+':store',session_cookie='sushi_store',
            max_age=8*3600,same_site='none' if config.COOKIE_SECURE else 'lax',https_only=config.COOKIE_SECURE)

    async def __call__(self, scope, receive, send):
        path = scope.get('path','')
        target = self.admin if path=='/admin' or path.startswith('/admin/') else self.public
        await target(scope,receive,send)
