# 🍓 Raspberry Pi

[← Вікі](README.md) · [Рація на iPhone](iphone-radio.md)

Pi — дім системи: ефір і передача для iPhone, розпізнавання через Whisper на Mac, бот, панель, API Mini App.
Без USB-карти ефір не запускається, решта працює. Вузол ефіру: [server/radio_node.py](../server/radio_node.py).

## Що потрапляє на Pi

Лише файли зі списку [deploy/profiles/pi.files](../deploy/profiles/pi.files); його будує `.venv/bin/python -m tools.profiles --write`
за імпортами процесів, повноту перевіряє `pytest`. Приватне (`.env`, база, зразки сигналів) копіюється окремо.

## Система

**Raspberry Pi OS Lite (64-bit) на Debian 13 Trixie** — без робочого столу.
Без PipeWire/PulseAudio звук іде напряму через ALSA, ніхто не перехоплює USB-карту.
Trixie має Python 3.13 і libgpiod 2. На Python 3.11 зі старішої Bookworm numpy з
`deploy/requirements/pi.txt` може не встановитися.

Монітор не потрібен. У Raspberry Pi Imager перед записом карти («Edit settings»):

- ім’я хоста, наприклад `radio345`; користувач — будь-який звичайний;
- SSH за ключем. Якщо ключа на Mac немає:
  `ssh-keygen -t ed25519 -C radio345`, потім `ssh-add --apple-use-keychain ~/.ssh/id_ed25519`.
  В Imager вставляйте лише `.pub`;
- Wi-Fi з країною (без неї Wi-Fi не ввімкнеться) або кабель Ethernet. Біля
  передавача кабель надійніший. Для 5 ГГц канал роутера 36–48: канали 52–144 (DFS)
  роутер мусить кидати на 1–10 хв, якщо «почує» радар, і перший запуск на них довгий.
  Налаштування мережі з Imager застосовуються лише під час першого запуску; далі —
  `nmcli` чи `sudo nmtui`;
- часовий пояс;
- Raspberry Pi Connect не вмикайте: віддалений доступ дає Tailscale.

Перший вхід із Mac у тій самій мережі: `ssh pi@radio345.local`. Після
`sudo tailscale up --ssh` Pi доступна з будь-якої мережі.

Для роботи цілодобово (стан на Trixie, перевірено 22.11.2026):

- журнал уже в пам’яті за замовчуванням (`journalctl --header` → `/run/log/journal`),
  log2ram не потрібен;
- апаратний watchdog уже ввімкнено: `systemctl show -p RuntimeWatchdogUSec` → `1min`.
  Система, що зависла, перезавантажиться сама;
- `sudo apt full-upgrade` після встановлення й `sudo apt install unattended-upgrades`
  (типово — лише оновлення безпеки);
- підкачка лише в пам’яті (zram), без файла `/var/swap` на карті (на 8 ГБ звільняє 2 ГБ):
  `printf '[Main]\nMechanism=zram\n' | sudo tee /etc/rpi/swap.conf.d/80-zram-only.conf`
  (спершу `sudo mkdir -p /etc/rpi/swap.conf.d`), перезавантаження, `losetup -l` порожній →
  `sudo rm -f /var/swap`;
- `swapon`, `losetup`, `iw` лежать у `/usr/sbin`: без sudo — повний шлях;
- батарейка RTC (ML2032), щоб після ввімкнення без мережі був правильний час;
- overlay FS (лише читання) — пізніше, після налагодження: захищає карту від
  раптового вимкнення живлення, але заважає оновленням.

## Встановлення й оновлення

На Pi один раз: `sudo apt install -y git ffmpeg libportaudio2 python3-venv`, Tailscale (`sudo tailscale up --ssh`).
Код і залежності передає Mac:

```bash
cd ~/Documents/Projects/radio
L=$(mktemp); grep -v '^#' deploy/profiles/pi.files | sed 's#^/##' > $L
rsync -ar --files-from=$L ./ <користувач>@radio345:radio345-hub/
```

На Pi:

```bash
cd ~/radio345-hub
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r deploy/requirements/hub.txt
sudo sh deploy/hub/install.sh            # і після оновлення коду: змінюються команди служб
```

`.env` на Pi: `RADIO_ASR_URL` (вузол Whisper на Mac), `RADIO_INPUT_DEVICE=USB`, `RADIO_PTT`, токен бота. Бот має працювати
лише на одному пристрої. Бази з Mac — `deploy/hub/import.sh`.
Стан: `systemctl is-active radio345-node radio345-panel radio345-asr`, журнал: `journalctl -u radio345-asr -f`.
Pi пускає лише пристрої вашого облікового запису Tailscale (не ставте Pi тег).
У застосунку: Налаштування → «Pi», адреса — ім'я Pi в Tailscale (напр. `radio345`).

## Перевірено на Pi

22.11.2026, Pi 5 4 ГБ, без USB-карти — через віртуальну `snd-aloop` і 25 хв ефіру з архіву
([звіт](reports/2026-11-22-pi-node-load.md)):

- навантаження: вузол ~0,9 % одного ядра й 45 МБ, три ffmpeg разом ~1,5 %; 61 °C після хвилини
  `stress-ng` на 4 ядрах, без троттлінгу;
- захоплення на Linux іде зі змінним блоком і нарізається на кадри в Python: фіксований блок
  PortAudio/ALSA втрачав ~60 % звуку (`input overflow`, ривки на iPhone).

Пробний ефір без карти: `sudo modprobe snd-aloop`, у `.env` `RADIO_INPUT_DEVICE=hw:2,1`
(номер — з `shared.audio_devices`), перезапуск служби, `aplay -D plughw:2,0 запис.wav`.

## Ще не перевірено на Pi

- USB-карта на 16 кГц під ALSA; можливо, знадобиться пристрій `plughw`/`default` з перетворенням частоти.
- gpiod v2 і номер чипа GPIO на Pi 5.
- Скидання лінії PTT при падінні процесу.
- PTT підключайте лише через оптопару чи транзистор, з увімкненим TOT рації.

## Mini App на Pi

API слухає лише `127.0.0.1:8765`; назовні його відкриває ngrok на домені з `RADIO345_NGROK_URL`. Панель і ефір не публікуються.
Домен той самий, що на Mac, тож сторінка й бот не змінюються, але **одночасно тунель працює лише на одному пристрої**:
спершу зупиніть `r345` із тунелем на Mac.

1. Установити ngrok (Linux arm64, [ngrok.com/download](https://ngrok.com/download)), під звичайним користувачем `ngrok config add-authtoken …`.
2. Додати в `.env` на Pi `RADIO345_NGROK_URL=https://…`.
3. `sudo sh deploy/hub/install.sh --miniapp`; закрити — `sudo sh deploy/hub/install.sh --no-miniapp`.

Без `--miniapp` служби API й тунелю не встановлюються. Mini App показує базу **цього** пристрою: бази Mac і Pi не синхронізуються.

## Керування з панелі Mac

Кнопка «Оновити код Pi»: Pi бере з GitHub коміт, на якому працює Mac (запушений і без змін), перевіряє й за збою повертає попередній.

Один раз на Pi: `sudo sh deploy/hub/control.sh` (прибрати — `--remove`). Дозволяє без пароля лише `enable --now`, `disable --now` і
`restart` для служб `radio345-*`. На Mac у `.env`: `RADIO345_PI_SSH=<користувач>@radio345`. Панель: «Система» → «Пристрої».

## Бекап баз на Mac

`.venv/bin/python -m tools.pull_backup --host jackrv@radio345` на Mac: знімок `events.db` та індексу архіву,
перевірка сум і цілісності, 14 останніх у `~/Library/Application Support/radio345/pi-backups`. Аудіо не копіюється.

Розклад (на Mac): та сама команда з `--install-agent`; вимкнути — `--remove-agent`, стан — `--status`. Щогодини перевіряє,
чи є копія, молодша за 20 год; Pi недоступна — спробує знову, понад 3 доби без копії — сповіщення macOS (раз на добу).
Скрипт копіюється в `~/Library`, бо фонові процеси macOS не мають доступу до `~/Documents`; після оновлення коду повторіть
`--install-agent`. SSH-ключ має працювати без введення пароля. Копії не шифруються.
