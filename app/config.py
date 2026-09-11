import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / '.env')
DATA = Path(os.getenv('DATA_DIR', str(ROOT / 'data'))).resolve()
MEDIA = DATA / 'media'
DB = DATA / 'shop.sqlite3'
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
ADMIN_IDS = [int(x.strip()) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip()]
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD_HASH = os.getenv('ADMIN_PASSWORD_HASH', '')
SESSION_SECRET = os.getenv('SESSION_SECRET', '')
COOKIE_SECURE = os.getenv('COOKIE_SECURE', 'false').lower() == 'true'
SHOP_NAME = os.getenv('SHOP_NAME', 'Осень Кусна')
CURRENCY = os.getenv('CURRENCY', '₽')


def prepare():
    DATA.mkdir(parents=True, exist_ok=True)
    MEDIA.mkdir(parents=True, exist_ok=True)
