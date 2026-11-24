#!/bin/bash
# Рація 345 · перезбирання й встановлення на iPhone (безкоштовний підпис діє 7 днів).
#
#   tools/ios_reinstall.sh                 — зібрати й поставити, якщо підпис закінчується (≤ RENEW_DAYS)
#   tools/ios_reinstall.sh --force         — зібрати й поставити зараз
#   tools/ios_reinstall.sh --build         — лише зібрати й підписати, без iPhone
#   tools/ios_reinstall.sh --status        — вік встановлення й стан агента
#   tools/ios_reinstall.sh --devices       — показати пристрої (ID для RADIO345_IOS_DEVICE)
#   tools/ios_reinstall.sh --install-agent — перевірка через launchd (спроба на добу після HOUR; строк минув — щогодини)
#   tools/ios_reinstall.sh --remove-agent  — прибрати launchd-агента
#
# Агент працює зі знімком застосунку в ~/Library: до ~/Documents macOS фонові процеси не пускає.
# Знімок оновлюють --install-agent і кожне вдале ручне встановлення (якщо агента ввімкнено).
#
# ID iPhone: змінна RADIO345_IOS_DEVICE або файл .local-ios/device-id.
# Потрібно один раз: Apple ID у Xcode → Settings → Accounts, apps/ios/Config/Signing.local.xcconfig,
# iPhone сполучений із Xcode (Window → Devices, «Connect via network») і Developer Mode увімкнено.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE="$ROOT/.local-ios"   # або .local-data/ios після r345 move-data
if [[ ! -d "$STATE" && -d "$ROOT/.local-data" ]]; then STATE="$ROOT/.local-data/ios"; fi
MIN_AGE_DAYS="${MIN_AGE_DAYS:-5}"   # лише коли строк підпису невідомий (встановлено старим скриптом)
RENEW_DAYS="${RENEW_DAYS:-2}"
HOUR="${HOUR:-5}"
LABEL="local.radio345.ios-reinstall"
AGENT="$HOME/Library/LaunchAgents/$LABEL.plist"
SNAPSHOT="${RADIO345_IOS_SNAPSHOT:-$HOME/Library/Application Support/radio345/ios}"
mkdir -p "$STATE"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

sync_snapshot() {
  [[ "$ROOT" == "$SNAPSHOT" ]] && return 0
  (umask 077; mkdir -p "$SNAPSHOT/tools" "$SNAPSHOT/apps/ios" "$SNAPSHOT/.local-ios")
  chmod 700 "$SNAPSHOT"
  rsync -a --delete --exclude xcuserdata "$ROOT/apps/ios/" "$SNAPSHOT/apps/ios/"
  cp "$ROOT/tools/ios_reinstall.sh" "$SNAPSHOT/tools/ios_reinstall.sh"
  printf '%s\n' "$DEVICE" > "$SNAPSHOT/.local-ios/device-id"
  if [[ "$STATE/last-install" -nt "$SNAPSHOT/.local-ios/last-install" ]]; then
    cp -p "$STATE/last-install" "$SNAPSHOT/.local-ios/last-install"
  fi
}

# Строк профілю в секундах Unix; порожньо — невідомо. Нова збірка не завжди означає новий строк:
# Xcode бере наявний профіль, доки той діє.
profile_expiry() {
  local value
  value="$(security cms -D -i "$1/embedded.mobileprovision" 2>/dev/null | plutil -extract ExpirationDate raw - 2>/dev/null)" || return 0
  date -j -u -f '%Y-%m-%dT%H:%M:%SZ' "$value" +%s 2>/dev/null || true
}

installed_expiry() {
  local value
  value="$(cat "$STATE/last-install" 2>/dev/null || true)"
  if [[ "$value" =~ ^[0-9]+$ ]]; then echo "$value"; fi
}

require_setup() {
  if [[ -z "$DEVICE" ]]; then
    log "Не задано iPhone: запишіть ID з 'tools/ios_reinstall.sh --devices' у .local-ios/device-id"
    exit 1
  fi
  if [[ ! -f "$ROOT/apps/ios/Config/Signing.local.xcconfig" ]]; then
    log "Немає apps/ios/Config/Signing.local.xcconfig (DEVELOPMENT_TEAM і RADIO345_BUNDLE_ID)"
    exit 1
  fi
}

# Агент не має ламатися мовчки: підпис діє 7 днів.
notify_failure() {
  local code=$?
  if [[ $code -ne 0 && "$(cat "$STATE/last-warn" 2>/dev/null || true)" != "$(date +%F)" ]]; then
    date +%F > "$STATE/last-warn"   # щогодинні спроби — одне сповіщення на добу
    osascript -e 'display notification "Не вдалося оновити підпис застосунку на iPhone" with title "Radio 345"' >/dev/null 2>&1 || true
  fi
  return 0
}

DEVICE="${RADIO345_IOS_DEVICE:-$(cat "$STATE/device-id" 2>/dev/null || true)}"

case "${1:-}" in
  --devices)
    exec xcrun devicectl list devices
    ;;
  --status)
    seen=""
    for mark in "$STATE/last-install" "$SNAPSHOT/.local-ios/last-install"; do
      if [[ -f "$mark" && ( -z "$seen" || "$mark" -nt "$seen" ) ]]; then seen="$mark"; fi
    done
    if [[ -n "$seen" ]]; then echo "Встановлено: $(( ($(date +%s) - $(stat -f %m "$seen")) / 86400 )) дн. тому"; else echo "Встановлено: немає даних"; fi
    expiry="$(cat "$seen" 2>/dev/null || true)"
    if [[ "$expiry" =~ ^[0-9]+$ ]]; then echo "Підпис діє до: $(date -r "$expiry" '+%d.%m.%Y %H:%M')"; fi
    if [[ ! -f "$AGENT" ]]; then echo "Агент: вимкнено"
    elif ! grep -qF "$SNAPSHOT/tools/" "$AGENT"; then echo "Агент: старий, без доступу до ~/Documents; повторіть --install-agent"
    else echo "Агент: увімкнено"; fi
    exit 0
    ;;
  --install-agent)
    require_setup
    sync_snapshot
    cat > "$AGENT" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$SNAPSHOT/tools/ios_reinstall.sh</string><string>--scheduled</string></array>
  <key>StartInterval</key><integer>3600</integer>
  <key>StandardOutPath</key><string>$SNAPSHOT/.local-ios/reinstall.log</string>
  <key>StandardErrorPath</key><string>$SNAPSHOT/.local-ios/reinstall.log</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin</string></dict>
</dict>
</plist>
PLIST
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$AGENT"
    log "Агент встановлено: перевірка щогодини, оновлення за $RENEW_DAYS дн. до кінця підпису. Журнал: $SNAPSHOT/.local-ios/reinstall.log"
    exit 0
    ;;
  --remove-agent)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$AGENT"
    log "Агента прибрано"
    exit 0
    ;;
  --scheduled) trap notify_failure EXIT ;;
  --force|--build|"") ;;
  *) echo "Невідомий параметр: $1" >&2; exit 2 ;;
esac

if [[ "${1:-}" != "--force" && "${1:-}" != "--build" && -f "$STATE/last-install" ]]; then
  EXPIRY="$(installed_expiry)"
  LEFT=0
  if [[ -n "$EXPIRY" ]]; then
    LEFT=$((EXPIRY - $(date +%s)))
    if (( LEFT > RENEW_DAYS * 86400 )); then
      [[ "${1:-}" == "--scheduled" ]] || log "Підпис ще діє, пропускаю"
      exit 0
    fi
  elif [[ -z "$(find "$STATE/last-install" -mtime +"$((MIN_AGE_DAYS - 1))" 2>/dev/null)" ]]; then
    [[ "${1:-}" == "--scheduled" ]] || log "Останнє встановлення свіже (< $MIN_AGE_DAYS дн.), пропускаю"
    exit 0
  fi
  # Діючий підпис: одна спроба на добу, не вночі. Строк минув — щогодини.
  if [[ "${1:-}" == "--scheduled" && ( -z "$EXPIRY" || "$LEFT" -gt 0 ) ]]; then
    TODAY="$(date +%F)"
    if (( 10#$(date +%H) < HOUR )) || [[ "$(cat "$STATE/last-try" 2>/dev/null || true)" == "$TODAY" ]]; then
      trap - EXIT
      exit 0
    fi
    echo "$TODAY" > "$STATE/last-try"
  fi
fi

require_setup
DESTINATION="platform=iOS,id=$DEVICE"
if [[ "${1:-}" == "--build" ]]; then DESTINATION="generic/platform=iOS"; fi

log "Збираю Release для iPhone…"
xcodebuild -quiet \
  -project "$ROOT/apps/ios/Radio345.xcodeproj" -scheme Radio345 -configuration Release \
  -destination "$DESTINATION" -derivedDataPath "$STATE/build" \
  -allowProvisioningUpdates -allowProvisioningDeviceRegistration \
  build

if [[ "${1:-}" == "--build" ]]; then
  log "Зібрано й підписано; на iPhone не встановлювалося"
  exit 0
fi

APP="$STATE/build/Build/Products/Release-iphoneos/Radio345.app"
NEW_EXPIRY="$(profile_expiry "$APP")"
if [[ "${1:-}" != "--force" && -n "$NEW_EXPIRY" && "$NEW_EXPIRY" == "$(installed_expiry)" ]]; then
  log "Xcode ще не продовжив підпис; застосунок на iPhone не чіпаю"
  exit 0
fi
log "Встановлюю на ${DEVICE}…"
if ! xcrun devicectl device install app --device "$DEVICE" "$APP"; then
  log "Встановлення не вдалося: iPhone недоступний (інша мережа, вимкнений, заблокований після перезавантаження)."
  log "Повтор — під час наступного запуску."
  exit 1
fi
printf '%s' "$NEW_EXPIRY" > "$STATE/last-install"
if [[ -f "$AGENT" && -d "$SNAPSHOT" ]]; then sync_snapshot; fi
log "Готово. Відкрийте Рацію 345 на iPhone, якщо слухали: встановлення зупиняє застосунок."
