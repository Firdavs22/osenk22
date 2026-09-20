"""Credentials never enter public settings, HTML values, or log messages."""
import json
import os

from cryptography.fernet import Fernet

from . import config, db


def cipher():
    config.prepare()
    path = config.DATA / 'integrations.key'
    if not path.exists():
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'wb') as f:
                f.write(Fernet.generate_key())
    return Fernet(path.read_bytes())


def seal(value):
    return cipher().encrypt(json.dumps(value, ensure_ascii=False).encode()).decode()


def unseal(value):
    return json.loads(cipher().decrypt(value.encode()))


def get_config(name, c=None):
    if c is None:
        with db.connect() as conn:
            return get_config(name, conn)
    row = c.execute('SELECT config FROM integrations WHERE name=?', (name,)).fetchone()
    return unseal(row['config']) if row else {}


def set_config(name, value):
    with db.connect(True) as c:
        c.execute('INSERT INTO integrations VALUES (?,?) ON CONFLICT(name) DO UPDATE SET config=excluded.config', (name, seal(value)))
