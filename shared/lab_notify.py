"""Короткий підсумок тесту лабораторії адміністраторам у Telegram (після кожного тесту черги).

Порівняння — з найсвіжішим завершеним прогоном «Як зараз у приймачі» (q-base) у тій самій теці.
Збій надсилання не зупиняє чергу.
"""
import html
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("lab_notify")


def _summary(run_dir):
    try:
        return json.loads((Path(run_dir) / "metrics.json").read_text())["summary"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _latest_base(root, exclude=None):
    runs = sorted((p for p in Path(root).iterdir() if p.is_dir() and p.name.endswith("_q-base")
                   and (p / "metrics.json").is_file() and p != exclude), reverse=True)
    return runs[0] if runs else None


def _pct(v):
    return "—" if v is None else f"{v * 100:.1f}".replace(".", ",") + " %"


def summary_text(root, title, status, run_dir, minutes, progress=None):
    head = f"🧪 Лабораторія: «{title}»"
    done = (f" — готово {progress['done']} з {progress['total']} записів" if progress and progress.get("total") else "")
    if status == "paused":
        return f"⏸ {head} — пауза{done}. Розпізнавання ефіру відновлюється; «Продовжити» — у панелі."
    if status == "window_paused":
        return f"⏸ {head} — закінчилось вікно{done}. Продовжиться з того ж місця в наступному вікні."
    after = next_line(root)
    if status != "done" or not run_dir:
        word = {"cancelled": "перервано", "failed": "помилка"}.get(status, status)
        if status in ("cancelled", "failed") and progress and progress.get("done"):
            word += f" ({progress['done']} з {progress.get('total') or '?'} записів збережено — можна продовжити)"
        return "\n".join(filter(None, [f"{head} — {word}. Подробиці в панелі: Аналіз → Експерименти.", after]))
    run_dir = Path(root) / run_dir
    s = _summary(run_dir)
    if not s:
        return "\n".join(filter(None, [f"{head} — завершено, але звіт не прочитано. Див. панель.", after]))
    base_dir = _latest_base(root, exclude=Path(run_dir))
    b = _summary(base_dir) if base_dir and not Path(run_dir).name.endswith("_q-base") else None

    def pair(key, fmt):
        value = fmt(s.get(key))
        return f"{value} (база {fmt(b.get(key))})" if b else value

    speech = s.get("speech_records") or 0
    lines = [f"{head} — {minutes} хв, мовлення {speech}",
             f"Без правок: {pair('exact_phrases', lambda v: '—' if v is None else str(v))}",
             f"WER: {pair('wer', _pct)} · номери: {pair('digits_exact', _pct)}",
             f"Пропущено мовлення: {pair('missed_speech', str)} · вигадки на шумі/сигналах: {pair('false_text_on_non_speech', str)}"]
    if not b and not Path(run_dir).name.endswith("_q-base"):
        lines.append("Бази для порівняння немає — спершу «Як зараз у приймачі».")
    return "\n".join(filter(None, [*lines, after]))


def start_text(title, minutes, left, resumed=False):
    tail = f" · далі ще {left}" if left else ""
    word = "продовження" if resumed else "старт"
    return (f"▶️ Лабораторія: {word} «{title}», орієнтовно {minutes} хв{tail}.\n"
            "Ефір лише записується; сповіщення ефіру — після тестів.")


def next_line(root):
    """«Далі …» для підсумку: наступний тест черги (його окремий старт не надсилається)."""
    try:
        from shared import lab_queue
        queued = [j for j in lab_queue.read(root)["jobs"] if j["status"] == "queued"]
    except Exception:
        return ""
    if not queued:
        return "✅ Черга завершена; розпізнавання ефіру відновлюється."
    title = lab_queue.PRESETS.get(queued[0]["preset"], {}).get("title", queued[0]["preset"])
    more = f" (у черзі ще {len(queued) - 1})" if len(queued) > 1 else ""
    return f"▶️ Далі: «{title}» — старт за ~{lab_queue.COOLDOWN_S // 60} хв{more}."


def send(text, project_root):
    """Надіслати адміністраторам і дочекатися (до 5 хв), щоб процес черги не завершився раніше."""
    from dotenv import load_dotenv
    import config
    from shared.telegram_common import TelegramBase
    load_dotenv(Path(project_root) / ".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return False
    thread = TelegramBase(token, 16000, config.DB_PATH, log).notify_admin_async(html.escape(text))
    if thread is not None:
        thread.join(timeout=300)
    return True
