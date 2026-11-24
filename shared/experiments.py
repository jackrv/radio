"""Офлайн-прогони лабораторії (.local-experiments) для локальної панелі: лише читання."""
import json
import re
from pathlib import Path

NAME = re.compile(r"[0-9]{8}T[0-9]{6}Z_[A-Za-z0-9._-]{1,80}")
KEYS = ("exact_phrases", "wer", "cer", "digits_exact", "app_numbers_exact", "recipient_accuracy_from_hypothesis",
        "sender_accuracy_from_hypothesis", "missed_speech", "false_text_on_non_speech",
        "false_text_on_room_noise", "latency_p50_s", "latency_p90_s", "speech_records", "records",
        "non_speech_records", "room_records", "errors")
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
            with (path / "results.jsonl").open(encoding="utf-8") as source:
                done = sum(1 for line in source if line.strip())
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
    """Метрики двох прогонів лише на спільних записах (як lab.asr_lab compare)."""
    from shared.lab_results import load_run, summarize as metrics_for
    _, ra = load_run(_run_dir(root, a))
    _, rb = load_run(_run_dir(root, b))
    from shared.text_metrics import metric_words, edit_distance
    by_a, by_b = {r["id"]: r for r in ra}, {r["id"]: r for r in rb}
    shared = by_a.keys() & by_b.keys()
    # Новіша розмітка — інший еталон, а не поліпшення моделі.
    def reference(row):
        return (row.get("label"), *(tuple(metric_words(row.get(key))) if row.get(key) is not None else None
                                   for key in ("reference", "reference_recipient", "reference_sender")))
    common = {key for key in shared if reference(by_a[key]) == reference(by_b[key])}
    sa = metrics_for([r for r in ra if r["id"] in common])
    sb = metrics_for([r for r in rb if r["id"] in common])
    changed = []
    for key in sorted(common):
        old, new = by_a[key], by_b[key]
        if old.get("label") != "speech":
            continue
        truth = metric_words(old.get("reference"))
        delta = edit_distance(truth, metric_words(new.get("hypothesis"))) - edit_distance(truth, metric_words(old.get("hypothesis")))
        if delta:
            changed.append({"id": key, "delta": delta})
    return {"common": len(common), "excluded_reference": len(shared) - len(common),
            "only_a": len(by_a.keys() - shared), "only_b": len(by_b.keys() - shared),
            "better": sum(c["delta"] < 0 for c in changed), "worse": sum(c["delta"] > 0 for c in changed),
            "examples": sorted(changed, key=lambda c: (c["delta"] < 0, -abs(c["delta"])))[:20],
            "a": {k: sa.get(k) for k in KEYS}, "b": {k: sb.get(k) for k in KEYS}}


def report(root, name):
    """Підсумок одного прогону й порівняння з найсвіжішою завершеною базою."""
    _run_dir(root, name)
    runs = list_runs(root, limit=200)
    run = next((r for r in runs if r["name"] == name), None)
    if run is None:
        raise LookupError("Прогін не знайдено у списку останніх 200")
    base = next((r for r in runs if r["label"] == "q-base" and r["complete"] and r["name"] != name), None)
    comparison = compare_runs(root, base["name"], name) if base and run["complete"] else None
    return {"run": run, "baseline": base["name"] if base else None, "comparison": comparison}
