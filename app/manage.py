import argparse
import getpass
import os
import secrets
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from contextlib import closing

from dotenv import dotenv_values

from . import config, db
from .security import hash_password


def setup():
    print('Настройка магазина. Секреты сохраняются только в локальный .env.')
    token = getpass.getpass('Токен бота от @BotFather (ввод скрыт): ').strip()
    if ':' not in token or not token.split(':')[0].isdigit():
        raise SystemExit('Неверный формат токена')
    ids = input('Telegram ID администраторов через запятую: ').strip()
    if not ids or any(not part.strip().isdigit() for part in ids.split(',')):
        raise SystemExit('Укажите числовые ID пользователей Telegram')
    username = input('Логин веб-админки [admin]: ').strip() or 'admin'
    password = getpass.getpass('Пароль веб-админки (минимум 12 символов): ')
    if password != getpass.getpass('Повторите пароль: '):
        raise SystemExit('Пароли не совпадают')
    encoded = hash_password(password)
    env = config.ROOT / '.env'
    if not env.exists():
        env.write_text((config.ROOT / '.env.example').read_text(encoding='utf-8'), encoding='utf-8')
    values = dict(dotenv_values(env))
    values.update(BOT_TOKEN=token, ADMIN_IDS=ids, ADMIN_USERNAME=username,
                  ADMIN_PASSWORD_HASH=encoded, SESSION_SECRET=secrets.token_urlsafe(48))
    def quote(value):
        return "'" + str(value or '').replace('\\', '\\\\').replace("'", "\\'") + "'"
    # The installer grants write access to .env, while keeping the code directory read-only.
    env.write_text('\n'.join(f'{key}={quote(value)}' for key, value in values.items()) + '\n', encoding='utf-8')
    if os.name != 'nt':
        env.chmod(0o600)
    db.init()
    print('Готово. Дальше загрузите пример меню (seed) или добавьте свои товары в админке.')
    print('При повторной настройке перезапустите оба сервиса.')


def backup(output=None):
    db.init()
    folder = Path(output).resolve() if output else config.DATA / 'backups'
    folder.mkdir(parents=True, exist_ok=True)
    name = 'sushi-backup-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f') + '.zip'
    target = folder / name
    # SQLite backup API copies a consistent live database, including WAL transactions.
    with tempfile.TemporaryDirectory(dir=config.DATA) as temporary:
        snapshot = Path(temporary) / 'shop.sqlite3'
        with db.connect() as source, closing(sqlite3.connect(snapshot)) as dest:
            source.backup(dest)
        with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.write(snapshot, 'shop.sqlite3')
            key = config.DATA / 'integrations.key'
            if key.exists():
                archive.write(key, 'integrations.key')
            for path in config.MEDIA.glob('*.jpg'):
                archive.write(path, 'media/' + path.name)
    if os.name != 'nt':
        target.chmod(0o600)
    print(f'Резервная копия: {target}')
    print('В архив входят база и фото. .env храните отдельно. Переносите копии за пределы VPS.')
    return target


def main():
    parser = argparse.ArgumentParser(description='Управление магазином суши')
    parser.add_argument('command', choices=['setup', 'init', 'seed', 'password', 'secret', 'backup', 'check'])
    parser.add_argument('--output', help='Папка для резервных копий')
    args = parser.parse_args()
    if args.command == 'setup':
        setup()
    elif args.command == 'init':
        db.init()
        print('База данных создана. Приём заказов по умолчанию выключен.')
    elif args.command == 'seed':
        db.seed()
        print('Пример меню добавлен, если каталог был пуст. Проверьте цены и состав перед продажами.')
    elif args.command == 'password':
        print(hash_password(getpass.getpass('Новый пароль (минимум 12 символов): ')))
    elif args.command == 'secret':
        print(secrets.token_urlsafe(48))
    elif args.command == 'backup':
        backup(args.output)
    elif args.command == 'check':
        db.init()
        with db.connect() as c:
            print('SQLite:', c.execute('PRAGMA integrity_check').fetchone()[0])
        print('BOT_TOKEN:', 'задан' if config.BOT_TOKEN else 'НЕ ЗАДАН')
        print('ADMIN_IDS:', 'заданы' if config.ADMIN_IDS else 'НЕ ЗАДАНЫ')
        print('Пароль:', 'задан' if config.ADMIN_PASSWORD_HASH.startswith('scrypt$') else 'НЕ ЗАДАН')
        print('Секрет сессии:', 'задан' if len(config.SESSION_SECRET) >= 32 else 'НЕ ЗАДАН')
        print('Приём заказов:', 'открыт' if db.settings()['orders_open'] == '1' else 'закрыт')


if __name__ == '__main__':
    main()
