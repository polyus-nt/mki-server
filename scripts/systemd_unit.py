"""Печатает unit для текущего пользователя и текущего расположения репозитория."""
from pathlib import Path
import os
import pwd
import sys


def quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def render(root, user):
    return f"""[Unit]
Description=FabLab greenhouse MSTJUK FastAPI
After=network.target
StartLimitIntervalSec=0

[Service]
Type=exec
User={user}
SupplementaryGroups=dialout
WorkingDirectory={str(root).replace('%', '%%')}
Environment=PYTHONUNBUFFERED=1
Environment=MSTJUK_SERIAL_PORT=auto
Environment=MSTJUK_HTTP_HOST=0.0.0.0
Environment=MSTJUK_HTTP_PORT=8000
EnvironmentFile=-/etc/default/mstjuk-api
ExecStart={quote(root / '.venv/bin/python')} -m hub_service
Restart=always
RestartSec=5
TimeoutStopSec=20
KillSignal=SIGINT

[Install]
WantedBy=multi-user.target
"""


def main():
    if os.getuid() == 0:
        sys.exit("Запускайте генератор без sudo: unit должен работать от вашего пользователя")
    root = Path(__file__).resolve().parents[1]
    if not (root / '.venv/bin/python').exists():
        sys.exit("Сначала создайте .venv и установите requirements.txt в корне репозитория")
    print(render(root, pwd.getpwuid(os.getuid()).pw_name), end="")


if __name__ == '__main__':
    main()
