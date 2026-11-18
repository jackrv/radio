"""Офлайн-прогони лабораторії (.local-experiments) для локальної панелі: лише читання."""
import json
import re
from pathlib import Path

NAME = re.compile(r"[0-9]{8}T[0-9]{6}Z_[A-Za-z0-9._-]{1,80}")
KEYS = ("exact_phrases", "wer", "cer", "digits_exact", "app_numbers_exact", "recipient_accuracy_from_hypothesis",
        "sender_accuracy_from_hypothesis", "missed_speech", "false_text_on_non_speech",
        "false_text_on_room_noise", "latency_p50_s", "latency_p90_s", "speech_records", "records")
PARAMS = ("variant", "prompt", "model", "llm", "temperature", "min_rms_gate", "bandpass", "resegment",
          "whisper_options", "records", "labelled_only", "statuses", "started_utc", "pipeline_scope",
          "daily_codes", "retry", "names_file", "whisper_phrases", "split")


def _run_dir(root, name):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError("Некоректна назва прогону")
    path = Path(root) / name
    if path.is_symlink() or not (path / "params.json").is_file():
        raise LookupError("Прогін не знайдено")
    return path


def list_runs(root, limit=40):
    root = Path(root)
    runs = []
    if not root.is_dir():
        return runs
    for path in sorted((p for p in root.iterdir() if NAME.fullmatch(p.name) and p.is_dir()), reverse=True)[:limit]:
        try:
            params = json.loads((path / "params.json").read_text())
        except (OSError, ValueError):
            continue
        summary = None
        try:
            summary = json.loads((path / "metrics.json").read_text()).get("summary")
        except (OSError, ValueError):
            pass
        try:
            done = sum(1 for line in (path / "results.jsonl").open(encoding="utf-8") if line.strip())
        except OSError:
            done = 0
        shown = {k: params.get(k) for k in PARAMS if k in params}
        for key in ("names_file", "whisper_phrases"):   # шлях до приватного файлу не показуємо — лише «свій список»
            if key in shown:
                shown[key] = bool(shown[key])
        runs.append({"name": path.name, "label": path.name.split("_", 1)[1],
                     "params": shown,
                     "done": done, "complete": summary is not None and done >= (params.get("records") or 0),
                     "summary": {k: summary.get(k) for k in KEYS if k in summary} if summary else None})
    return runs


def compare_runs(root, a, b):
    """Метрики двох прогонів лише на спільних записах (як tools.asr_lab compare)."""
    from services.lab_results import load_run, summarize as metrics_for
    _, ra = load_run(_run_dir(root, a))
    _, rb = load_run(_run_dir(root, b))
    common = {r["id"] for r in ra} & {r["id"] for r in rb}
    sa = metrics_for([r for r in ra if r["id"] in common])
    sb = metrics_for([r for r in rb if r["id"] in common])
    return {"common": len(common), "a": {k: sa.get(k) for k in KEYS}, "b": {k: sb.get(k) for k in KEYS}}
