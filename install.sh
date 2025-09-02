#!/usr/bin/env bash
set -euo pipefail

echo "==> WINGSBOT_FREE installer"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python3 not found. Please install Python 3.10+ and retry."; exit 1
fi
if ! command -v pip3 >/dev/null 2>&1; then
  echo "pip3 not found. Please install pip and retry."; exit 1
fi

WORKDIR="$(cd "$(dirname "$0")" && pwd)"
cd "$WORKDIR"

echo "==> Creating virtualenv .venv"
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip

REQ_FILE=requirements.txt
if [ ! -f "$REQ_FILE" ]; then
  echo "Generating requirements.txt ..."
  cat > requirements.txt <<'REQ'
python-telegram-bot==20.7
httpx==0.27.0
requests==2.32.3
qrcode==7.4.2
REQ
fi

echo "==> Installing dependencies"
pip install -r requirements.txt

ENV_FILE=.env
if [ ! -f "$ENV_FILE" ]; then
  echo "==> Creating .env"
  read -rp "Enter BOT_TOKEN: " BOT_TOKEN
  read -rp "Enter ADMIN_ID (numeric): " ADMIN_ID
  read -rp "Enter CHANNEL_ID (numeric or @username): " CHANNEL_ID
  cat > .env <<EOF
BOT_TOKEN=${BOT_TOKEN}
ADMIN_ID=${ADMIN_ID}
CHANNEL_ID=${CHANNEL_ID}
EOF
fi

echo "==> Initializing database"
python - <<'PY'
from bot.db import db_setup
db_setup()
print('DB ready')
PY

echo "==> Creating systemd unit (optional)"
SERVICE_FILE=wingsbot.service
cat > ${SERVICE_FILE} <<'UNIT'
[Unit]
Description=WINGSBOT_FREE Telegram Bot
After=network.target

[Service]
WorkingDirectory=%h/WINGSBOT_FREE
EnvironmentFile=%h/WINGSBOT_FREE/.env
ExecStart=%h/WINGSBOT_FREE/.venv/bin/python -m bot.run
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

echo "==> Done. To run now:"
echo "source .venv/bin/activate && python -m bot.run"

