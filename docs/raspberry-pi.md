# 🍓 Радіовузол на Raspberry Pi

[← Вікі](README.md) · [Рація на iPhone](iphone-radio.md)

Pi поруч із рацією знімає звук, віддає потоки на iPhone і передає з iPhone в ефір із ключем PTT.
Розпізнавання тут немає: «Відібрані» порожні, а сегменти поки відкидаються (див. [ідеї](ideas.md)).
Код: [services/radio_node.py](../services/radio_node.py).

## Що потрапляє на Pi

Лише файли з [deploy/pi-files.txt](../deploy/pi-files.txt): код вузла, сторінка для Safari, залежності,
служба й ця інструкція. Список не редагують вручну. Після змін коду:

```bash
.venv/bin/python -m tools.pi_files --write
```

`pytest` (`tests/test_pi_files.py`) падає, якщо вузол імпортує файл, якого немає в списку.
Приватне (`.env`, зразки сигналів, токен) у git не потрапляє, на Pi його копіюють окремо.

## Система

**Raspberry Pi OS Lite (64-bit) на Debian 13 Trixie** — без робочого столу.
Без PipeWire/PulseAudio звук іде напряму через ALSA, ніхто не перехоплює USB-карту.
Trixie має Python 3.13 і libgpiod 2. На Python 3.11 зі старішої Bookworm numpy з
`requirements-pi.txt` може не встановитися.

Монітор не потрібен. У Raspberry Pi Imager перед записом карти («Edit settings»):

- ім’я хоста, наприклад `radio345`; користувач `pi` (його очікує служба);
- SSH за ключем. Якщо ключа на Mac немає:
  `ssh-keygen -t ed25519 -C radio345`, потім `ssh-add --apple-use-keychain ~/.ssh/id_ed25519`.
  В Imager вставляйте лише `.pub`;
- Wi-Fi з країною (без неї Wi-Fi не ввімкнеться) або кабель Ethernet. Біля
  передавача кабель надійніший;
- часовий пояс;
- Raspberry Pi Connect не вмикайте: віддалений доступ дає Tailscale.

Перший вхід із Mac у тій самій мережі: `ssh pi@radio345.local`. Після
`sudo tailscale up --ssh` Pi доступна з будь-якої мережі.

Для роботи цілодобово:

- менше запису на карту: `Storage=volatile` у `/etc/systemd/journald.conf`
  (журнал у пам’яті, після перезавантаження зникає) або `log2ram`;
- `sudo apt install unattended-upgrades`, лише оновлення безпеки;
- апаратний watchdog: `RuntimeWatchdogSec=15` у `/etc/systemd/system.conf`.
  Система, що зависла, перезавантажиться сама;
- батарейка RTC (ML2032), щоб після ввімкнення без мережі був правильний час;
- overlay FS (лише читання) — пізніше, після налагодження: захищає карту від
  раптового вимкнення живлення, але заважає оновленням.

## Встановлення (один раз)

```bash
sudo apt install git ffmpeg libportaudio2 python3-venv
# Tailscale: https://tailscale.com/download/linux, потім sudo tailscale up

git clone --filter=blob:none --sparse https://github.com/jackrv/radio.git ~/radio345
cd ~/radio345
git sparse-checkout set --no-cone --stdin < <(git show HEAD:deploy/pi-files.txt)
python3 -m venv .venv && .venv/bin/pip install -r requirements-pi.txt
.venv/bin/python -m services.audio_devices        # знайти назву USB-карти
```

Для приватного репозиторію потрібен deploy key лише на читання (GitHub → Settings → Deploy keys).

`.env` на Pi — лише налаштування вузла, без токена Telegram:

```
RADIO_INPUT_DEVICE=USB
RADIO_OUTPUT_DEVICE=USB
RADIO_PTT=gpio:17
```

Зразки сигналів (за бажанням) — з Mac: `scp .local-notification-sounds/*.wav pi@<pi>:radio345/.local-notification-sounds/`.

Служба:

```bash
sudo cp deploy/radio345-node.service /etc/systemd/system/
sudo systemctl enable --now radio345-node
journalctl -u radio345-node -f
```

Токен для iPhone створюється в `~/radio345/.local-radio-stream-token`. У застосунку
вкажіть адресу Pi в Tailscale замість адреси Mac.

## Оновлення

```bash
cd ~/radio345 && git pull
git sparse-checkout set --no-cone --stdin < deploy/pi-files.txt   # якщо список змінився
.venv/bin/pip install -r requirements-pi.txt
sudo systemctl restart radio345-node
```

## Ще не перевірено на Pi

- USB-карта на 16 кГц під ALSA; можливо, знадобиться пристрій `plughw`/`default` з перетворенням частоти.
- gpiod v2 і номер чипа GPIO на Pi 5.
- Скидання лінії PTT при падінні процесу.
- Навантаження: VAD, фільтр сигналів і два кодувальники ffmpeg.
- PTT підключайте лише через оптопару чи транзистор, з увімкненим TOT рації.
