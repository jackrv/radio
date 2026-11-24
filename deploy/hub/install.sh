#!/bin/sh
# Встановлення служб на Pi: sudo sh deploy/hub/install.sh (повторювати можна).
# --units — лише оновити файли встановлених служб. --miniapp — лише увімкнути API Mini App і публічний тунель; --no-miniapp — закрити їх. Інші служби не чіпає.
set -eu
[ "$(id -u)" = 0 ] || { echo "Потрібен sudo"; exit 1; }
[ "$(uname -s)" = Linux ] || { echo "Лише для Raspberry Pi (Linux)"; exit 1; }
root=$(cd "$(dirname "$0")/../.." && pwd)
user=${SUDO_USER:?запускайте через sudo від звичайного користувача}
[ "$user" != root ] && [ -x "$root/.venv/bin/python" ] || { echo "Немає $root/.venv або користувача"; exit 1; }
case "${1:-}" in
    "") units="radio345-node radio345-panel radio345-asr" ;;
    --miniapp)
        units="radio345-api radio345-tunnel"
        [ -s "$root/events.db" ] || { echo "Немає бази events.db"; exit 1; }
        for key in TELEGRAM_BOT_TOKEN RADIO345_NGROK_URL; do
            grep -q "^$key=..*" "$root/.env" 2>/dev/null || { echo "У .env бракує $key"; exit 1; }
        done
        command -v ngrok >/dev/null || { echo "Не знайдено ngrok"; exit 1; }
        sudo -u "$user" -H ngrok config check >/dev/null 2>&1 || { echo "ngrok не налаштовано: ngrok config add-authtoken …"; exit 1; } ;;
    --units)   # лише файли вже встановлених служб, без запуску й увімкнення
        for unit in node panel asr api tunnel; do
            [ -f "/etc/systemd/system/radio345-$unit.service" ] || continue
            sed "s|@USER@|$user|g; s|@ROOT@|$root|g" "$root/deploy/hub/radio345-$unit.service" > "/etc/systemd/system/radio345-$unit.service"
        done
        systemctl daemon-reload
        echo "Файли служб оновлено; перезапустіть потрібні"; exit 0 ;;
    --no-miniapp)
        systemctl disable --now radio345-tunnel radio345-api 2>/dev/null || true
        echo "Mini App на Pi закрито"; exit 0 ;;
    *) echo "Невідомий параметр: $1"; exit 2 ;;
esac
for unit in $units; do
    sed "s|@USER@|$user|g; s|@ROOT@|$root|g" "$root/deploy/hub/$unit.service" > "/etc/systemd/system/$unit.service"
done
systemctl daemon-reload
systemctl enable $units
systemctl restart $units
systemctl --no-pager --lines=3 status $units || true
