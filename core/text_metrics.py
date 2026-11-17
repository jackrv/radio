"""Єдині метрики тексту для архіву, повтору тексту й лабораторії.

Слова для WER: без регістру й розділових знаків; апостроф будь-якого виду не ділить
і не змінює слово («зв’язок», «звʼязок» і «звязок» — одне слово). Алиаси й інші
виправлення тут не застосовуються: метрика оцінює сам текст.
"""
import re

APOSTROPHES = "’'ʼ`"
_APOSTROPHE = re.compile(f"[{APOSTROPHES}]")
_WORD = re.compile(r"[^\W_]+")


def metric_words(text):
    return _WORD.findall(_APOSTROPHE.sub("", (text or "").casefold()))


def edit_distance(a, b):
    from rapidfuzz.distance import Levenshtein
    return Levenshtein.distance(a, b)


def error_rates(reference, hypothesis):
    """Помилки слів і символів гіпотези відносно еталона (WER/CER — None без еталона)."""
    ref, hyp = metric_words(reference), metric_words(hypothesis)
    ref_chars, hyp_chars = " ".join(ref), " ".join(hyp)
    words, chars = edit_distance(ref, hyp), edit_distance(ref_chars, hyp_chars)
    return {"word_errors": words, "reference_words": len(ref), "wer": words / len(ref) if ref else None,
            "char_errors": chars, "reference_chars": len(ref_chars),
            "cer": chars / len(ref_chars) if ref_chars else None}
