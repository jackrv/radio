#!/bin/bash
# Рація 345 · перезбирання й встановлення на iPhone (безкоштовний підпис діє 7 днів).
#
#   tools/ios_reinstall.sh                 — зібрати й поставити, якщо минуло ≥ MIN_AGE_DAYS
#   tools/ios_reinstall.sh --force         — зібрати й поставити зараз
#   tools/ios_reinstall.sh --devices       — показати пристрої (ID для RADIO345_IOS_DEVICE)
#   tools/ios_reinstall.sh --install-agent — щоденна перевірка через launchd (о HOUR:30)
#   tools/ios_reinstall.sh --remove-agent  — прибрати launchd-агента
#
# ID iPhone: змінна RADIO345_IOS_DEVICE або файл .local-ios/device-id.
# Потрібно один раз: Apple ID у Xcode → Settings → Accounts, apps/ios/Config/Signing.local.xcconfig,
# iPhone сполучений із Xcode (Window → Devices, «Connect via network») і Developer Mode увімкнено.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE="$ROOT/.local-ios"
MIN_AGE_DAYS="${MIN_AGE_DAYS:-5}"
HOUR="${HOUR:-5}"
LABEL="local.radio345.ios-reinstall"
AGENT="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$STATE"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

case "${1:-}" in
  --devices)
    exec xcrun devicectl list devices
    ;;
  --install-agent)
    cat > "$AGENT" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$ROOT/tools/ios_reinstall.sh</string></array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>30</integer></dict>
  <key>StandardOutPath</key><string>$STATE/reinstall.log</string>
  <key>StandardErrorPath</key><string>$STATE/reinstall.log</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin</string></dict>
</dict>
</plist>
PLIST
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$AGENT"
    log "Агент встановлено: щодня о $HOUR:30, перевстановлення раз на $MIN_AGE_DAYS дн. Журнал: $STATE/reinstall.log"
    exit 0
    ;;
  --remove-agent)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$AGENT"
    log "Агента прибрано"
    exit 0
    ;;
  --force|"") ;;
  *) echo "Невідомий параметр: $1" >&2; exit 2 ;;
esac

if [[ "${1:-}" != "--force" && -f "$STATE/last-install" ]]; then
  if [[ -z "$(find "$STATE/last-install" -mtime +"$((MIN_AGE_DAYS - 1))" 2>/dev/null)" ]]; then
    log "Останнє встановлення свіже (< $MIN_AGE_DAYS дн.), пропускаю"
    exit 0
  fi
fi

DEVICE="${RADIO345_IOS_DEVICE:-$(cat "$STATE/device-id" 2>/dev/null || true)}"
if [[ -z "$DEVICE" ]]; then
  log "Не задано iPhone: запишіть ID з 'tools/ios_reinstall.sh --devices' у .local-ios/device-id"
  exit 1
fi
if [[ ! -f "$ROOT/apps/ios/Config/Signing.local.xcconfig" ]]; then
  log "Немає apps/ios/Config/Signing.local.xcconfig (DEVELOPMENT_TEAM і RADIO345_BUNDLE_ID)"
  exit 1
fi

log "Збираю Release для iPhone…"
xcodebuild -quiet \
  -project "$ROOT/apps/ios/Radio345.xcodeproj" -scheme Radio345 -configuration Release \
  -destination "platform=iOS,id=$DEVICE" -derivedDataPath "$STATE/build" \
  -allowProvisioningUpdates -allowProvisioningDeviceRegistration \
  build

APP="$STATE/build/Build/Products/Release-iphoneos/Radio345.app"
log "Встановлюю на ${DEVICE}…"
if ! xcrun devicectl device install app --device "$DEVICE" "$APP"; then
  log "Встановлення не вдалося: iPhone недоступний (інша мережа, вимкнений, заблокований після перезавантаження)."
  log "Повтор — під час наступного запуску."
  exit 1
fi
touch "$STATE/last-install"
log "Готово. Відкрийте Рацію 345 на iPhone, якщо слухали: встановлення зупиняє застосунок."
