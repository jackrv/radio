# Радио 345

Локальная система распознавания речевых сообщений
с обнаружением позывных, SQLite и Telegram.

## Окружение

- macOS, Apple Silicon
- Python 3.12
- VS Code
- MLX Whisper large-v3-turbo
- Qwen2.5-3B-Instruct-4bit

## Установка

```bash
python3.12 -m venv venv
source venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .env.example .env
```

Заполнить TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID
в локальном файле .env.

Для отправки голосовых сообщений в формате OGG
дополнительно требуется ffmpeg.

## Первоначальное заполнение словарей

Рабочие словари хранятся в SQLite (`events.db`).

При первой установке подготовьте три файла:

- `callsigns.txt` — позывные;
- `commands.txt` — команды;
- `telegram.txt` — список наблюдения.

Формат строк должен соответствовать формату,
поддерживаемому `callsigns.py`.

Для первоначального импорта выполните:

    python -c "from config import DB_PATH; from database import init_db, import_dictionaries; init_db(DB_PATH); import_dictionaries('.', DB_PATH); print('Словари импортированы')"

Импорт выполняется только в незаполненные категории.
Если хотя бы один словарь уже содержит записи,
операция отменяется без перезаписи данных.

После импорта TXT-файлы для запуска не требуются.
Программа читает словари непосредственно из SQLite.

**Важно:** не удаляйте рабочую `events.db` ради
повторного импорта. В ней также хранятся события,
обученные исправления и настройки Telegram.

Локальные TXT-файлы, SQLite и `.env` исключены из Git.

## Запуск

```bash
python -u radio.py
```

Остановка: Ctrl+C.

## Диагностика аудио

```bash
RADIO_DEBUG_AUDIO=1 python -u radio.py
```

Записи сохраняются в debug_audio/.
Эти файлы не предназначены для публикации.

## Тестирование

```bash
python -m pytest -q
```

## Архитектура

### Точка входа

- radio.py — инициализация приложения
- config.py — параметры
- callsigns.py — позывные и числительные
- database.py — хранение словарей в SQLite

### core/

- audio.py — подготовка звука
- audio_debug.py — диагностика
- vad.py — выделение речевых сегментов
- recognition.py — Whisper
- correction.py — Qwen
- pipeline.py — обработка текста
- analyzer.py — проверка результатов
- event_processor.py — обработка событий
- transcriber.py — цикл распознавания
- transcriber_factory.py — сборка обработчика
- supervisor.py — управление потоками
- restart_policy.py — политика перезапуска
- bounded_queue.py — ограниченная очередь
- queue_monitor.py — мониторинг очередей
- cooldown.py — защита от повторов

### services/

- telegram.py — уведомления и команды бота
- event_store.py — события и исправления SQLite
- dictionary_loader.py — загрузка словарей
- bot_state.py — состояние Telegram

### tests/

Автоматические тесты компонентов.

## Обработка сообщений

Аудиовход → VAD → Whisper → коррекция текста
→ обнаружение позывных/команд → SQLite → Telegram.

## Данные и безопасность

- events.db содержит рабочие данные.
- .env содержит секреты Telegram.
- radio.log и debug_audio/ могут содержать сообщения.
- Эти файлы исключены из Git.
- Основные словари хранятся в SQLite.

## Конфигурация

Рабочие параметры находятся в config.py.
Временные диагностические функции выключены
по умолчанию.
