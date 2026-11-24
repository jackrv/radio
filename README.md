<div align="center">

<img src="docs/assets/logo.svg" width="120" alt="Radio 345">

# Radio 345

**Локальне розпізнавання радіоефіру з миттєвими сповіщеннями в Telegram**

[![Tests](https://github.com/jackrv/radio/actions/workflows/tests.yml/badge.svg)](https://github.com/jackrv/radio/actions/workflows/tests.yml)
[![Mini App](https://github.com/jackrv/radio/actions/workflows/miniapp-pages.yml/badge.svg)](https://github.com/jackrv/radio/actions/workflows/miniapp-pages.yml)
<br>
![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![macOS Apple Silicon](https://img.shields.io/badge/macOS-Apple%20Silicon-000000?logo=apple&logoColor=white)
![Whisper large-v3](https://img.shields.io/badge/Whisper-large--v3%20·%20MLX-8cc4ff)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-003B57?logo=sqlite&logoColor=white)
![Telegram](https://img.shields.io/badge/Telegram-Bot%20%2B%20Mini%20App-26A5E4?logo=telegram&logoColor=white)
![Мова](https://img.shields.io/badge/мова-українська-b8ef86)

[Вікі](docs/README.md) · [Встановлення](docs/install.md) · [Панель](docs/panel.md) · [Розмітка](docs/archive.md) · [Лабораторія](docs/lab.md)

</div>

---

Radio 345 слухає рацію на Raspberry Pi, розпізнає мовлення **локально на вашому Mac** і за кілька секунд надсилає кожному учаснику голосове з текстом —
з позивними, командами й «кому / від» у шапці. Оператор виправляє тексти в
Mini App чи локальній панелі, і система вчиться на цих правках, **не вигадуючи
чисел і позивних**.

<p align="center"><img src="docs/assets/scheme.svg" alt="Схема: рація — дім на Pi або Mac — Telegram, панель, iPhone; Whisper завжди на Mac" width="860"></p>

## ✨ Можливості

| | |
| --- | --- |
| 🎙️ **Розпізнавання на пристрої** | MLX Whisper large-v3 на Apple Silicon, WebRTC VAD, підказка зі словників |
| 📚 **Словники ефіру** | позивні з дозволеними номерами, команди, коди доби, аліаси — зміни без перезапуску |
| 🛡️ **Безпечні виправлення** | шаблони зворотів і цілих фраз, числа ніколи не підміняються |
| 🧠 **Навчання за правками** | правки оператора стають перевіреними словесними правилами |
| 🔔 **Фільтр сигналів** | звуки сповіщень телефонів відкидаються ще до Whisper |
| 💬 **Telegram** | особисті режими, ролі, доступ через `/start`, оновлення й видалення копій у всіх |
| 🖥️ **Локальна панель** | розмітка архіву, підказки «Потребує уваги», якість по днях, експерименти |
| 🩺 **Надійність** | черга фраз на Pi, самовідновлення, щоденні копії бази, діагностика сесій |

## 🚀 Старт

[Встановлення](docs/install.md), потім `./r345 up --profile mac-worker` на Mac; на Pi — [служби](docs/raspberry-pi.md).
Панель на Mac: `.venv/bin/python -m server.api.local_dashboard --open`.

## 📖 Документація

| | |
| --- | --- |
| 🚀 [Встановлення](docs/install.md) | `.env`, словники, перший адміністратор |
| 🧭 [Як усе влаштовано](docs/system.md) | Pi, Mac, iPhone |
| ⚙️ [Запуск і служби](docs/launcher.md) | `r345`, systemd |
| 🆘 [Якщо щось не так](docs/troubleshooting.md) | симптоми й перевірки |
| 📻 [iPhone](docs/iphone-radio.md) | канали, передача, підпис |
| 🍓 [Pi](docs/raspberry-pi.md) | встановлення, оновлення, копії |
| 🎙️ [Розпізнавання](docs/recognition.md) | етапи конвеєра, безпека чисел, формат передач |
| 📚 [Словники й фільтри](docs/dictionaries.md) | коди доби, аліаси, шум, шаблони фраз |
| 🧠 [Навчання](docs/learning.md) | які правки стають правилами |
| 🎧 [Аудіоархів](docs/archive.md) | розмітка, клавіші, метрики, зберігання |
| 🔔 [Фільтр сигналів](docs/signal-filter.md) | зразки, пороги, перевірка на розмітці |
| 🖥️ [Локальна панель](docs/panel.md) | розділи, «Потребує уваги», доступ |
| 💬 [Telegram і Mini App](docs/telegram.md) | режими, ролі, правки, видалення |
| 💾 [Дані й копії](docs/data.md) | резервні копії, відновлення, скидання |
| 🩺 [Діагностика](docs/diagnostics.md) | метрики, затримки, історія сесій |
| 🧪 [Лабораторія](docs/lab.md) | офлайн-порівняння й результати дослідів |

## 🗂️ Структура

```text
config.py                 базові параметри
manage_database.py        імпорт, експорт, скидання й відновлення бази
diagnose_radio345.py      перевірка без зупинки приймача
r345, radio345.py         запуск процесів за профілем пристрою (deploy/profiles)
shared/                   спільне: аудіо, VAD, розбір тексту, словники, архів, SQLite
server/                   ефір, розпізнавання з ботом, панель, API
worker/                   вузол Whisper
lab/                      лабораторія: asr_lab, text_replay, analysis, навчання
server/management/        керування з панелі й Mini App: словники, навчання, користувачі, налаштування
server/api/               FastAPI для Mini App; local_dashboard.py — вхід і безпека панелі
server/api/panel/         ендпоінти панелі за розділами (огляд, архів, розпізнавання, сигнали, люди, система)
local_admin/              сторінка панелі; js/ — скрипт за розділами (сервер склеює їх в один /admin.js)
apps/miniapp/             Mini App (HTML/CSS/JS без збирання, GitHub Pages)
tools/                    обслуговування: профілі, бекап Pi, перевірки
tests/                    pytest і node-тести на тимчасових даних
docs/                     вікі
```

## 🧪 Тести

```bash
.venv/bin/python -m pytest -q
node --test tests/*.cjs
```

Тести працюють на тимчасових базах, синтетичному аудіо й підміненому Telegram —
без моделей і мікрофона; робочу `events.db` відкрити не можуть. GitHub Actions
запускає їх на `macos-latest`.

## 🔒 Приватність

`.env`, `dictionaries.txt`, `events.db`, журнали, копії, аудіо, діагностика й
каталоги `.local-*` **ніколи не потрапляють у Git**. У репозиторії — лише код,
документація й шаблони з вигаданими назвами. Детальніше — [Дані й копії](docs/data.md).
