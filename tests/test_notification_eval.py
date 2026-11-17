"""Оцінка фільтра сигналів на розмітці: поріг і ліміт сторонньої енергії разом."""
from types import SimpleNamespace

from services.notification_eval import _summary


def item(label, score, extra, outside=0.0):
    return {"label": label, "score": score, "extra_ratio": extra, "outside_s": outside}


def test_noisy_signals_lead_to_a_looser_extra_limit_without_touching_speech():
    matcher = SimpleNamespace(threshold=0.80, max_outside_s=0.6, max_extra=0.06)
    signals = [item("notification", 0.85, 0.03)] * 3 + [item("notification", 0.82, 0.12)] * 7
    speech = [item("speech", 0.86, 0.05, outside=2.5)] * 10 + [item("speech", 0.5, 0.01)] * 2
    s = _summary(signals + speech, matcher)
    assert s["status"] == "ok" and s["current"]["notifications_found"] == 3
    assert s["best"]["max_extra"] == 0.15 and s["best"]["notifications_found"] == 10, \
        "найсуворіший ліміт, що ловить і зашумлені сигнали"
    assert all(e["notifications_found"] <= 10 for e in s["extras"])


def test_no_better_combination_when_signals_are_clean():
    matcher = SimpleNamespace(threshold=0.80, max_outside_s=0.6, max_extra=0.06)
    s = _summary([item("notification", 0.9, 0.01)] * 5 + [item("speech", 0.3, 0.2)] * 10, matcher)
    assert s["best"]["notifications_found"] == s["current"]["notifications_found"] == 5
    assert s["best"]["max_extra"] == 0.02
