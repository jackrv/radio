"""Розпізнавання на Pi окремо від ефіру: черга фраз → вузол Mac → архів, база.

Запуск: ``.venv/bin/python -m services.asr_service`` (потрібен RADIO_ASR_URL).
Mac недоступний або фраза стара — вона йде в архів як deferred і розпізнається пізніше без подій.
Бот працює тут, якщо на Pi задано TELEGRAM_BOT_TOKEN; водночас із лаунчером Mac його не запускати.
"""
import logging
import os
import queue
import signal
import threading
import time
from pathlib import Path

FRESH_S = 300
MAX_ATTEMPTS = 5


class NullTelegram:
    """Без токена: жодних повідомлень."""

    def recipients_for(self, wants):
        return []


class SpoolRecorder:
    """Синхронний запис в архів: файл черги видаляється лише після нього."""

    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        self.record_id = self.captured_at = None

    def submit(self, pcm, sr, data):
        self.store.save(pcm, sr, dict(data, captured_at=self.captured_at or time.time(), settings=self.settings),
                        record_id=self.record_id)


class AsrService:
    def __init__(self, spool, worker, recorder, log, fresh_s=FRESH_S):
        self.spool, self.worker, self.recorder, self.log, self.fresh_s = spool, worker, recorder, log, fresh_s
        self.attempts = {}

    def step(self):
        """Одна дія; True — була робота."""
        files = self.spool.files()
        if not files:
            return self.worker._process_one_deferred()
        # Свіжі — першими: накопичене не затримує поточний ефір.
        path = files[-1]
        try:
            record_id, pcm, rate, ended = self.spool.read(path)
        except FileNotFoundError:
            return True
        except ValueError:
            self.log.exception("Пошкоджена фраза в черзі: %s", path.name)
            self.spool.quarantine(path)
            return True
        self.recorder.record_id, self.recorder.captured_at = record_id, ended
        try:
            if self.recorder.store.exists(record_id):
                pass   # збій між записом в архів і видаленням файла
            elif time.time() - ended > self.fresh_s or self.attempts.get(record_id):
                self.worker._store_deferred(pcm)   # стара чи повторна: без подій
            else:
                self.worker.process_segment(pcm)
                if not self.recorder.store.exists(record_id):
                    raise OSError("Фразу не збережено в архів")
        except Exception:
            n = self.attempts[record_id] = self.attempts.get(record_id, 0) + 1
            self.log.exception("Фразу не оброблено (спроба %d)", n)
            if n >= MAX_ATTEMPTS:
                self.spool.quarantine(path)
                self.attempts.pop(record_id, None)
            return False
        finally:
            self.recorder.record_id = self.recorder.captured_at = None
        self.attempts.pop(record_id, None)
        path.unlink(missing_ok=True)
        return True

    def run(self, stop, tick=lambda: None):
        while not stop.is_set():
            tick()
            try:
                worked = self.step()
            except Exception:
                self.log.exception("Збій циклу розпізнавання")
                worked = False
            if not worked:
                stop.wait(1.0)


class Bot:
    """Слухач Telegram з автоперезапуском, обслуговування й коректне завершення."""

    def __init__(self, service, log):
        self.service, self.log, self.thread, self.next_start = service, log, None, 0.0

    def tick(self):
        self.service.start_maintenance()
        if (self.thread is None or not self.thread.is_alive()) and time.monotonic() >= self.next_start:
            if self.thread is not None:
                self.log.warning("Слухач Telegram зупинився; перезапуск")
            self.next_start = time.monotonic() + 10
            self.thread = threading.Thread(target=self.service.callback_listener, name="telegram-listener", daemon=True)
            self.thread.start()


def main():
    from dotenv import load_dotenv

    import config
    from core.cooldown import Cooldown
    from core.instance_lock import OWNER_LOCK, acquire_instance_lock
    from core.log_security import configure_logging
    from services.dictionary_loader import load_dictionaries
    from services.event_store import init_db
    from services.management.settings import apply_startup_settings
    from services.recordings import RecordingStore
    from services.segment_spool import SegmentSpool
    from services.transcriber_factory import build_transcriber

    load_dotenv(config.BASE_DIR / ".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    configure_logging(token, starting_message="Розпізнавання Pi через вузол Mac.")
    log = logging.getLogger("radio")
    if not os.environ.get("RADIO_ASR_URL", "").strip():
        raise SystemExit("Не задано RADIO_ASR_URL")
    # Не замок моделі: на Mac її поруч тримає services.asr_worker.
    lock = acquire_instance_lock(Path(config.BASE_DIR) / OWNER_LOCK)
    if lock is None:
        raise SystemExit("Інший приймач уже працює з цією базою")
    init_db(config.DB_PATH)
    apply_startup_settings(config.DB_PATH, config)
    store = RecordingStore(config.RECORDINGS_DIR, retention_days=config.RECORDINGS_RETENTION_DAYS,
                           max_bytes=config.RECORDINGS_MAX_BYTES,
                           low_rms_retention_seconds=config.RECORDINGS_LOW_RMS_RETENTION_SECONDS)
    recorder = SpoolRecorder(store, {"MODEL_REPO": config.MODEL_REPO, "LANGUAGE": config.LANGUAGE, "SR": config.SR})
    bot, telegram = None, NullTelegram()
    if token:
        from services.telegram import TelegramService
        telegram = TelegramService(bot_token=token, sample_rate=config.SR, db_path=config.DB_PATH, log=log)
        telegram.archive_root = config.RECORDINGS_DIR
        bot = Bot(telegram, log)
    else:
        log.warning("Telegram вимкнено: не задано TELEGRAM_BOT_TOKEN у .env")
    callsigns, commands, watch, prompt = load_dictionaries(config.DB_PATH, log)
    worker = build_transcriber(
        config=config, segments=queue.Queue(), callsigns=callsigns, commands=commands, watch=watch,
        prompt=prompt, telegram_service=telegram, cooldown=Cooldown(config.COOLDOWN_S), log=log, recorder=recorder)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    signal.signal(signal.SIGINT, lambda *a: stop.set())
    log.info("Розпізнавання працює", extra={"console": True})
    from services.db_backup import start_backup_scheduler
    backup_stop = start_backup_scheduler(config.DB_PATH, config.BASE_DIR / ".local-backups", log=log)
    try:
        AsrService(SegmentSpool(spool_dir(config)), worker, recorder, log).run(stop, bot.tick if bot else lambda: None)
    finally:
        backup_stop.set()
        if bot:
            telegram.shutdown(timeout=10.0)
        os.close(lock)


def spool_dir(config):
    return os.environ.get("RADIO_SPOOL_DIR", "").strip() or Path(config.BASE_DIR) / ".local-segment-spool"


if __name__ == "__main__":
    main()
