#!/bin/sh
# Один раз: дозволити користувачу Pi без пароля лише перелічені дії зі службами radio345-*. Прибрати: sudo sh deploy/hub/control.sh --remove
set -eu
[ "$(id -u)" = 0 ] || { echo "Потрібен sudo"; exit 1; }
user=${SUDO_USER:?запускайте через sudo від звичайного користувача}
[ "$user" != root ] || { echo "Потрібен звичайний користувач"; exit 1; }
case "$user" in *[!a-z0-9_-]*) echo "Неочікуване ім'я користувача"; exit 1 ;; esac
target=/etc/sudoers.d/radio345-control
rules=/etc/polkit-1/rules.d/50-radio345.rules
if [ "${1:-}" = --remove ]; then rm -f "$target" "$rules"; echo "Прибрано"; exit 0; fi
temp=$(mktemp)
trap 'rm -f "$temp"' EXIT
for name in node asr panel api tunnel; do
    for verb in "enable --now" "disable --now" restart; do
        echo "$user ALL=(root) NOPASSWD: /usr/bin/systemctl $verb radio345-$name"
    done
done > "$temp"
visudo -cqf "$temp" || { echo "Правила не пройшли перевірку"; exit 1; }
install -o root -g root -m 0440 "$temp" "$target"
echo "Дозволено керування службами radio345-* з Mac"
# Панель Pi (NoNewPrivileges, без sudo): лише start/stop/restart цих служб через polkit; себе не зупиняє, автозапуску не міняє.
if [ -d /etc/polkit-1/rules.d ]; then
    cat > "$temp" <<RULE
polkit.addRule(function(action, subject) {
    if (action.id != "org.freedesktop.systemd1.manage-units" || subject.user != "$user") return;
    var unit = action.lookup("unit"), verb = action.lookup("verb");
    if (["node", "asr", "panel", "api", "tunnel"].map(function(n) { return "radio345-" + n + ".service"; }).indexOf(unit) < 0) return;
    if (["start", "stop", "restart"].indexOf(verb) < 0 || (unit == "radio345-panel.service" && verb == "stop")) return;
    return polkit.Result.YES;
});
RULE
    install -o root -g root -m 0644 "$temp" "$rules"
    echo "Дозволено керування службами radio345-* з панелі Pi"
else
    echo "polkit не встановлено: кнопки служб на панелі Pi не працюватимуть"
fi
