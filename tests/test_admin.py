import io
import re

import pytest
from PIL import Image

from app import config, db


def csrf(response):
    return re.search(r'name="csrf" value="([^"]+)"', response.text).group(1)


def login(client):
    token = csrf(client.get('/login'))
    response = client.post('/login', data={'csrf': token, 'username': 'admin', 'password': 'correct horse sushi 123'})
    assert response.status_code == 200 and 'Всё готово' in response.text
    return csrf(response)


def test_access_and_csrf(client):
    assert client.get('/products', follow_redirects=False).status_code == 303
    assert client.get('/media/anything.jpg', follow_redirects=False).status_code == 303
    assert client.post('/settings', data={}).status_code == 200  # redirected to login
    login(client)
    assert client.post('/products/1/toggle', data={'csrf': 'bad'}).status_code == 403
    assert db.product(1)['active'] == 1


def test_login_limit(client):
    token = csrf(client.get('/login'))
    for _ in range(5):
        client.post('/login', data={'csrf': token, 'username': 'admin', 'password': 'wrong'})
    response = client.post('/login', data={'csrf': token, 'username': 'admin', 'password': 'correct horse sushi 123'})
    assert 'Слишком много попыток' in response.text


def test_login_requires_session_csrf(client):
    assert client.post('/login', data={'csrf': '!', 'username': 'admin', 'password': 'correct horse sushi 123'}).status_code == 403
    client.get('/login')
    assert client.post('/login', data={'csrf': 'чужой токен'}).status_code == 403


def test_pages_render(client):
    login(client)
    for path in ['/', '/products', '/products/new', '/products/1', '/orders', '/settings', '/notifications']:
        result = client.get(path)
        assert result.status_code == 200, path
        assert 'script-src' in result.headers['content-security-policy']


def product_fields(token):
    return {'csrf': token, 'category_id': '1', 'name': '<script>alert(1)</script>',
            'price': '720.50', 'ingredients': 'Лосось, рис', 'weight': '280 г', 'description': 'Тест', 'active': 'on'}


def test_product_upload_and_xss_escape(client):
    token = login(client)
    content = io.BytesIO()
    Image.new('RGB', (200, 100), 'red').save(content, 'PNG')
    response = client.post('/products/save', data=product_fields(token), files={'photo': ('../../test.png', content.getvalue(), 'image/png')})
    assert response.status_code == 200
    assert '&lt;script&gt;' in response.text
    assert '<script>alert(1)</script>' not in response.text
    p = db.products()[0]
    assert p['price'] == 72050 and p['photo'].endswith('.jpg')
    assert client.get('/media/' + p['photo']).headers['content-type'] == 'image/jpeg'


def test_invalid_upload_does_not_create_product(client):
    token = login(client)
    result = client.post('/products/save', data=product_fields(token), files={'photo': ('fake.png', b'<html>not image</html>', 'image/png')})
    assert 'Не удалось прочитать' in result.text
    assert len(db.products()) == 5


def test_payload_size_limit(client):
    token = login(client)
    result = client.post('/products/save', data=product_fields(token), files={'photo': ('big.jpg', b'x' * (7*1024*1024), 'image/jpeg')})
    assert result.status_code == 413
    assert len(db.products()) == 5


def test_session_invalid_after_password_change(client, monkeypatch):
    login(client)
    monkeypatch.setattr(config, 'ADMIN_PASSWORD_HASH', 'scrypt$changed$' + 'f'*64)
    assert client.get('/', follow_redirects=False).status_code == 303


def test_settings_and_toggle(client):
    token = login(client)
    response = client.post('/settings', data={'csrf': token, 'shop_name': 'Тест суши', 'currency': '₽',
        'phone': '+79990000000', 'address': 'Москва, Примерная, 1', 'hours': '10–22',
        'delivery_fee': '150', 'minimum_order': '700', 'free_delivery_from': '2000', 'orders_open': 'on'})
    assert 'Настройки сохранены' in response.text
    assert db.settings()['delivery_fee'] == '15000'
    assert db.settings()['delivery_enabled'] == '0'
    client.post('/products/1/toggle', data={'csrf': token})
    assert db.product(1)['active'] == 0
