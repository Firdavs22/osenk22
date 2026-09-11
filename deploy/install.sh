#!/usr/bin/env bash
set -euo pipefail
# Run only after copying the project to /opt/sushi-bot on Ubuntu/Debian.
if [[ "$(id -u)" != "0" ]]; then
  echo "Запустите: sudo bash deploy/install.sh"
  exit 1
fi
cd /opt/sushi-bot
apt-get update
apt-get install -y python3 python3-venv python3-pip
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Требуется Python 3.11+. Используйте Ubuntu 24.04 или Debian 12+."'
if ! id sushi >/dev/null 2>&1; then
  useradd --system --home-dir /opt/sushi-bot --shell /usr/sbin/nologin sushi
fi
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.lock.txt
install -d -m 700 -o sushi -g sushi /opt/sushi-bot/data
if [[ ! -f .env ]]; then
  install -m 600 .env.example .env
fi
chown sushi:sushi .env
chmod 600 .env
install -m 644 deploy/sushi-web.service /etc/systemd/system/sushi-web.service
install -m 644 deploy/sushi-bot.service /etc/systemd/system/sushi-bot.service
install -m 644 deploy/sushi-backup.service /etc/systemd/system/sushi-backup.service
install -m 644 deploy/sushi-backup.timer /etc/systemd/system/sushi-backup.timer
systemctl daemon-reload
echo 'Установлено. Теперь выполните:'
echo 'sudo -u sushi .venv/bin/python -m app.manage setup'
echo 'sudo -u sushi .venv/bin/python -m app.manage seed'
echo 'sudo systemctl enable --now sushi-web sushi-bot sushi-backup.timer'
