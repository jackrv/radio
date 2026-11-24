"""Результати лабораторії (.local-experiments): читання прогону й підсумкові метрики.

Спільне для lab/asr_lab.py і сторінки «Аналіз → Експерименти» панелі.
"""
import json
from pathlib import Path

from shared.text_metrics import metric_words as words

plain_tokens = words


# Шум приміщення (клавіатура тощо) — артефакт тестової установки: окремо від шуму ефіру.
SPEECH, UNCLEAR, NON_SPEECH = ("speech",), ("unclear",), ("noise", "notification")
ROOM = ("room",)


# Характерні звороти: суцільні послідовності слів після вилучення апострофів.
# «чую … розбираю» — обидва слова в будь-якому місці.
FAMILIES = {
    "перевірка звʼязку": [("перевірка", "звязку")],
    "на звʼязку / на звʼязок": [("на", "звязку"), ("на", "звязок")],
    "чую … розбираю": "both:чую,розбираю",
    "до роботи приступив": [("до", "роботи", "приступив")],
    "роботу закінчив": [("роботу", "закінчив")],
    "посилку отримав": [("посилку", "отримав")],
    "прийнято / прийняв": [("прийнято",), ("прийняв",)],
    "прийом": [("прийом",)],
    "повторіть, голос не пройшов": [("повторіть", "голос", "не", "пройшов")],
}


def has_family(tokens, pattern):
    if isinstance(pattern, str):
        return all(word in tokens for word in pattern.split(":", 1)[1].split(","))
    for seq in pattern:
        n = len(seq)
        if any(tuple(tokens[i:i + n]) == seq for i in range(len(tokens) - n + 1)):
            return True
    return False


def role_tokens(value):
    return None if value is None else words(value)


def summarize(results):
    """Підсумок за розміченими записами; невідомі ролі не входять у знаменник."""
    speech = [r for r in results if r.get("label") in SPEECH and r.get("reference") is not None]
    summary = {"records": len(results), "labelled": sum(r.get("label") is not None for r in results)}
    if speech:
        errors = sum(r["score"]["word_errors"] for r in speech)
        total = sum(r["score"]["reference_words"] for r in speech)
        char_errors = sum(r["score"]["char_errors"] for r in speech)
        chars = sum(r["score"]["reference_chars"] for r in speech)
        with_digits = [r for r in speech if r["score"]["reference_has_digits"]]
        summary.update({
            "speech_records": len(speech),
            "wer": errors / total if total else None,
            "cer": char_errors / chars if chars else None,
            "missed_speech": sum(not (r.get("hypothesis") or "").strip() for r in speech),
            "digits_exact": (sum(r["score"]["digits_match"] for r in with_digits) / len(with_digits)) if with_digits else None,
            "app_numbers_exact": (sum(r["score"]["app_numbers_match"] for r in with_digits) / len(with_digits)) if with_digits else None,
        })
    non_speech = [r for r in results if r.get("label") in NON_SPEECH]
    if non_speech:
        summary["false_text_on_non_speech"] = sum(bool((r.get("hypothesis") or "").strip()) for r in non_speech)
        summary["non_speech_records"] = len(non_speech)
    room = [r for r in results if r.get("label") in ROOM]
    if room:
        summary["room_records"] = len(room)
        summary["false_text_on_room_noise"] = sum(bool((r.get("hypothesis") or "").strip()) for r in room)
    segments = [r["segments"] for r in results if isinstance(r.get("segments"), int)]
    if segments:
        summary["resegmented_records"] = len(segments)
        summary["segments_per_record_mean"] = sum(segments) / len(segments)
    for role in ("recipient", "sender"):
        # Два входи: автоматична розшифровка (слух + логіка) і правильний текст (лише логіка ролей).
        for source, key in (("hypothesis", f"hypothesis_{role}"), ("reference_text", f"reference_{role}_parsed")):
            pairs = [(r[f"reference_{role}"], r.get(key)) for r in results
                     if r.get("label") in SPEECH + UNCLEAR and r.get(f"reference_{role}") is not None
                     and r.get(key) is not None]
            if pairs:
                summary[f"{role}_accuracy_from_{source}"] = sum(role_tokens(a) == role_tokens(b) for a, b in pairs) / len(pairs)
                summary[f"{role}_evaluated"] = len(pairs)
    speech_ref = [r for r in results if r.get("label") in SPEECH and r.get("reference") is not None]
    if speech_ref:
        summary["exact_phrases"] = sum(plain_tokens(r["reference"]) == plain_tokens(r.get("hypothesis")) for r in speech_ref)
        summary['literal_phrases'] = sum(r['reference'] == (r.get('hypothesis') or '') for r in speech_ref)
    segs = lambda r: r.get("whisper_segments") or []
    summary["fallback_records"] = sum(any((s.get("temperature") or 0) > 0 for s in segs(r)) for r in results)
    summary["loop_records"] = sum(any((s.get("compression_ratio") or 0) > 2.4 for s in segs(r)) for r in results)
    negatives = [r for r in results if r.get("label") in NON_SPEECH + ROOM]
    families = {}
    for name, pattern in FAMILIES.items():
        in_ref = [r for r in speech_ref if has_family(plain_tokens(r["reference"]), pattern)]
        families[name] = {
            "in_reference": len(in_ref),
            "kept": sum(has_family(plain_tokens(r.get("hypothesis")), pattern) for r in in_ref),
            # Зворот з'явився там, де його не казали: ціна підказки.
            "invented_on_speech": sum(has_family(plain_tokens(r.get("hypothesis")), pattern)
                                      and not has_family(plain_tokens(r["reference"]), pattern) for r in speech_ref),
            "invented_on_negatives": sum(has_family(plain_tokens(r.get("hypothesis")), pattern) for r in negatives),
        }
    summary["families"] = families
    rtf = [r["timing"]["whisper_s"] / r["signal"]["duration_s"] for r in results
           if r.get("timing", {}).get("whisper_s") and (r.get("signal") or {}).get("duration_s")]
    if rtf:
        summary["rtf_p50"] = sorted(rtf)[len(rtf) // 2]
    summary["errors"] = sum("error" in r for r in results)
    timings = [r["timing"].get("total_s") for r in results if r.get("timing", {}).get("total_s") is not None]
    if timings:
        ordered = sorted(timings)
        summary["latency_p50_s"] = ordered[len(ordered) // 2]
        summary["latency_p90_s"] = ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]
    return summary


def load_run(run_dir):
    run_dir = Path(run_dir)
    results = [json.loads(line) for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines() if line]
    return json.loads((run_dir / "params.json").read_text()), results
