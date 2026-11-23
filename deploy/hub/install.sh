#!/bin/sh
# Встановлення служб на Pi: sudo sh deploy/hub/install.sh (повторювати можна).
set -eu
[ "$(id -u)" = 0 ] || { echo "Потрібен sudo"; exit 1; }
[ "$(uname -s)" = Linux ] || { echo "Лише для Raspberry Pi (Linux)"; exit 1; }
root=$(cd "$(dirname "$0")/../.." && pwd)
user=${SUDO_USER:?запускайте через sudo від звичайного користувача}
[ "$user" != root ] && [ -x "$root/.venv/bin/python" ] || { echo "Немає $root/.venv або користувача"; exit 1; }
for unit in radio345-node radio345-panel; do
    sed "s|@USER@|$user|g; s|@ROOT@|$root|g" "$root/deploy/hub/$unit.service" > "/etc/systemd/system/$unit.service"
done
systemctl daemon-reload
systemctl enable radio345-node radio345-panel
systemctl restart radio345-node radio345-panel
systemctl --no-pager --lines=3 status radio345-node radio345-panel || true
