# 🚀 Встановлення

[← Вікі](README.md)

Mac на Apple Silicon (16 ГБ), Python 3.12, `ffmpeg`, Tailscale. Pi — [окрема інструкція](raspberry-pi.md).

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
cp .env.example .env
cp dictionaries.example.txt dictionaries.txt      # позивні, команди, список спостереження
.venv/bin/python manage_database.py init --delete-source
.venv/bin/python -m server.api.bootstrap_admin    # перший адміністратор: TELEGRAM_CHAT_ID у .env
```

| `.env` | Навіщо |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | бот; лише на одному пристрої |
| `TELEGRAM_CHAT_ID` | реєстрація першого адміністратора |
| `RADIO345_PROFILE` | `mac-worker`, `mac-dev` або `pi` |
| `RADIO_ASR_URL` | на Pi: адреса вузла Whisper на Mac |
| `RADIO345_NGROK_URL` | домен тунелю Mini App |

Дайте терміналу доступ до мікрофона. Далі — [запуск](launcher.md).

## Формат словників

`слово` — без номера; `слово: 68 69`, `слово: 300-310` — номери; `слово: *` — будь-який; `слово 4242` — номер без двокрапки;
`#` — коментар. Розділи `[callsigns]`, `[commands]`, `[telegram]`. Далі словники редагуються в панелі; назад у файл — `manage_database.py export`.

## Звук

Краще кабель від рації в USB-карту, ніж мікрофон. Рівень мовлення −30…−15 dBFS без кліпінгу; пристрій вибирається в панелі
(«Система» → «Звукові пристрої»). Після зміни звуку заново оцініть пороги на розмітці.
