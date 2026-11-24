#!/bin/sh
# Один раз: дозволити користувачу Pi без пароля лише перелічені дії зі службами radio345-*. Прибрати: sudo sh deploy/hub/control.sh --remove
set -eu
[ "$(id -u)" = 0 ] || { echo "Потрібен sudo"; exit 1; }
user=${SUDO_USER:?запускайте через sudo від звичайного користувача}
[ "$user" != root ] || { echo "Потрібен звичайний користувач"; exit 1; }
case "$user" in *[!a-z0-9_-]*) echo "Неочікуване ім'я користувача"; exit 1 ;; esac
target=/etc/sudoers.d/radio345-control
if [ "${1:-}" = --remove ]; then rm -f "$target"; echo "Прибрано"; exit 0; fi
temp=$(mktemp)
trap 'rm -f "$temp"' EXIT
for name in node asr panel api tunnel; do
    for verb in "enable --now" "disable --now" restart; do
        echo "$user ALL=(root) NOPASSWD: /usr/bin/systemctl $verb radio345-$name"
    done
done > "$temp"
visudo -cqf "$temp" || { echo "Правила не пройшли перевірку"; exit 1; }
install -o root -g root -m 0440 "$temp" "$target"
echo "Дозволено керування службами radio345-* з панелі Mac"
