#!/bin/sh
# Замінити бази Pi підготовленим знімком: sudo sh deploy/hub/import.sh. Старі файли лишаються в .local-backups.
set -eu
[ "$(id -u)" = 0 ] || { echo "Потрібен sudo"; exit 1; }
root=$(cd "$(dirname "$0")/../.." && pwd)
src="$root/.import"
user=${SUDO_USER:?запускайте через sudo від звичайного користувача}
[ -s "$src/events.db" ] && [ -s "$src/index.sqlite3" ] || { echo "Немає знімка в $src"; exit 1; }
systemctl stop radio345-panel radio345-node radio345-asr 2>/dev/null || true
old="$root/.local-backups/before-import-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$old" "$root/.local-recordings"
for f in events.db events.db-wal events.db-shm .local-recordings/index.sqlite3 .local-recordings/index.sqlite3-wal .local-recordings/index.sqlite3-shm; do
    [ ! -e "$root/$f" ] || mv "$root/$f" "$old/"
done
mv "$src/events.db" "$root/events.db"
mv "$src/index.sqlite3" "$root/.local-recordings/index.sqlite3"
for d in .local-notification-sounds .local-selections .local-text-checks; do
    [ ! -d "$src/$d" ] || { mkdir -p "$root/$d"; cp -Rp "$src/$d/." "$root/$d/"; }
done
chown -R "$user:" "$root/events.db" "$root/.local-recordings" "$root/.local-backups" "$root/.local-notification-sounds" "$root/.local-selections" "$root/.local-text-checks"
chmod 700 "$root/.local-backups" "$root/.local-recordings"
chmod 600 "$root/events.db" "$root/.local-recordings/index.sqlite3"
sudo -u "$user" "$root/.venv/bin/python" -c "import sqlite3,sys
for p in sys.argv[1:]:
    d=sqlite3.connect(p); print(p.rsplit('/',1)[1], d.execute('PRAGMA integrity_check').fetchone()[0]); d.close()" "$root/events.db" "$root/.local-recordings/index.sqlite3"
systemctl start radio345-node radio345-panel
echo "Готово. Старі файли: $old"
