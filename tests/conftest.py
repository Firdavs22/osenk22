import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import config, db
from app.security import hash_password


@pytest.fixture
def shop(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DATA', tmp_path)
    monkeypatch.setattr(config, 'DB', tmp_path / 'shop.sqlite3')
    monkeypatch.setattr(config, 'MEDIA', tmp_path / 'media')
    monkeypatch.setattr(config, 'ADMIN_IDS', [999])
    monkeypatch.setattr(config, 'SESSION_SECRET', 'test-session-secret-with-at-least-32-characters')
    monkeypatch.setattr(config, 'ADMIN_PASSWORD_HASH', hash_password('correct horse sushi 123'))
    monkeypatch.setattr(config, 'ADMIN_USERNAME', 'admin')
    monkeypatch.setattr(config, 'COOKIE_SECURE', False)
    db.seed()
    with db.connect(True) as c:
        c.executemany('UPDATE settings SET value=? WHERE key=?', [('1', 'orders_open'), ('0', 'minimum_order'), ('г. Москва, улица Примерная, 1', 'address')])
    return db


@pytest.fixture
def client(shop):
    from fastapi.testclient import TestClient
    from app import admin
    importlib.reload(admin)
    with TestClient(admin.app) as client:
        yield client
