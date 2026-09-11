from dotenv import dotenv_values

from app import config
from app.manage import setup
from app.security import verify_password


def test_setup_saves_valid_env_without_plaintext_password(shop, monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    (tmp_path / '.env.example').write_text('COOKIE_SECURE=false\nDATA_DIR=./data\nSHOP_NAME=Осень Кусна\n', encoding='utf-8')
    hidden = iter(['123456:fake_token_for_setup_test', "strong sushi's password", "strong sushi's password"])
    visible = iter(['999,1000', 'owner'])
    monkeypatch.setattr('app.manage.getpass.getpass', lambda _: next(hidden))
    monkeypatch.setattr('builtins.input', lambda _: next(visible))
    setup()
    values = dotenv_values(tmp_path / '.env')
    assert values['ADMIN_IDS'] == '999,1000'
    assert values['ADMIN_USERNAME'] == 'owner'
    assert values['SHOP_NAME'] == 'Осень Кусна'
    assert values['COOKIE_SECURE'] == 'false'
    assert verify_password("strong sushi's password", values['ADMIN_PASSWORD_HASH'])
    assert len(values['SESSION_SECRET']) >= 32
    assert "strong sushi's password" not in (tmp_path / '.env').read_text(encoding='utf-8')
