"""Interactive local launcher. Run from START-WINDOWS.cmd or python start_local.py."""
import argparse
import hashlib
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import venv
import webbrowser

ROOT = Path(__file__).resolve().parent


def bootstrap():
    if sys.version_info < (3, 11):
        raise RuntimeError('Нужен Python 3.11 или новее.')
    environment = ROOT / '.venv'
    python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not python.is_file():
        print('Создаю виртуальное окружение…', flush=True)
        venv.EnvBuilder(with_pip=True).create(environment)
    lock = ROOT / 'requirements.lock.txt'
    fingerprint = hashlib.sha256(lock.read_bytes()).hexdigest()
    marker = environment / '.sushi-dependencies'
    if not marker.is_file() or marker.read_text().strip() != fingerprint:
        print('Устанавливаю зависимости из PyPI. Первый запуск может занять несколько минут.', flush=True)
        environment_vars = dict(os.environ, PIP_CONFIG_FILE=os.devnull)
        subprocess.run([str(python), '-m', 'pip', '--isolated', 'install', '--index-url',
                        'https://pypi.org/simple', '--disable-pip-version-check', '--keyring-provider',
                        'disabled', '-r', str(lock)], cwd=ROOT, env=environment_vars, check=True)
        marker.write_text(fingerprint, encoding='ascii')
    return subprocess.call([str(python), str(Path(__file__).resolve()), '--run', *sys.argv[1:]], cwd=ROOT)


def run(port):
    from dotenv import dotenv_values
    values = dotenv_values(ROOT / '.env')
    if not all(values.get(key) for key in ('BOT_TOKEN', 'ADMIN_IDS', 'ADMIN_PASSWORD_HASH', 'SESSION_SECRET')):
        print('\nПервый запуск. Подготовьте токен @BotFather и свой ID от @userinfobot.\n', flush=True)
        subprocess.run([sys.executable, '-m', 'app.manage', 'setup'], cwd=ROOT, check=True)
    from app import config, db
    if config.COOKIE_SECURE:
        raise RuntimeError('Локальный запуск использует HTTP. В локальном .env задайте COOKIE_SECURE=false. На VPS с HTTPS оставьте true.')
    with socket.socket() as probe:
        try:
            probe.bind(('127.0.0.1', port))
        except OSError:
            raise RuntimeError(f'Порт {port} уже занят. Закройте предыдущий запуск или используйте python start_local.py --port 8001.')
    db.seed()
    url = f'http://127.0.0.1:{port}'
    print('\nВитрина: ' + url, flush=True)
    print('Админка: ' + url + '/admin', flush=True)
    print('Войдите с логином и паролем, которые указали при настройке.', flush=True)
    print('В настройках укажите контакты и включите «Принимать заказы».', flush=True)
    print('Затем отправьте своему боту /start и оформите пробный заказ.', flush=True)
    print('Оставьте это окно открытым. Ctrl+C остановит бота и админку.\n', flush=True)
    processes = []
    windows_flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    logs = [('админка', config.DATA / 'local-web.log', ['-m', 'uvicorn', 'app.admin:app', '--host', '127.0.0.1', '--port', str(port), '--no-access-log']),
            ('бот', config.DATA / 'local-bot.log', ['-m', 'app.bot'])]
    try:
        for name, logpath, args in logs:
            with logpath.open('a', encoding='utf-8') as stream:
                process = subprocess.Popen([sys.executable, *args], cwd=ROOT, stdout=stream,
                                           stderr=subprocess.STDOUT, creationflags=windows_flags)
            processes.append((name, process, logpath))
        opened = False
        while True:
            for name, process, logpath in processes:
                if process.poll() is not None:
                    raise RuntimeError(f'Процесс «{name}» остановился. Подробности: {logpath}')
            if not opened:
                try:
                    with urllib.request.urlopen(url + '/health', timeout=1) as result:
                        if result.status == 200:
                            webbrowser.open(url)
                            opened = True
                except (OSError, TimeoutError):
                    pass
            time.sleep(1)
    except KeyboardInterrupt:
        print('\nОстанавливаю локальный запуск…', flush=True)
    finally:
        for _, process, _ in processes:
            if process.poll() is None:
                process.terminate()
        for _, process, _ in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == '__main__':
    os.chdir(ROOT)
    parser = argparse.ArgumentParser(description='Локальная проверка бота «Осень Кусна»')
    parser.add_argument('--run', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    try:
        if not 1024 <= args.port <= 65535:
            raise RuntimeError('Порт должен быть от 1024 до 65535')
        if args.run:
            run(args.port)
        else:
            raise SystemExit(bootstrap())
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print('\nОшибка: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
