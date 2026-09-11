import hashlib
import hmac
import secrets


def hash_password(password):
    if len(password) < 12:
        raise ValueError('Пароль должен содержать минимум 12 символов')
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return f'scrypt${salt}${digest}'


def verify_password(password, encoded):
    try:
        kind, salt, digest = encoded.split('$')
        if kind != 'scrypt' or len(password) > 1024:
            return False
        actual = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
        return hmac.compare_digest(actual, digest)
    except (ValueError, TypeError):
        return False
