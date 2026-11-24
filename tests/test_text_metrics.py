"""Одна метрика WER для архіву, повтору тексту й лабораторії."""
from shared.text_metrics import error_rates, metric_words
from shared import recordings, text_replay
from lab import asr_lab


def test_apostrophe_forms_are_one_word_and_punctuation_is_ignored():
    assert metric_words("Перевірка зв’язку, прийом!") == metric_words("перевірка звʼязку прийом") \
        == metric_words("перевірка звязку, прийом") == ["перевірка", "звязку", "прийом"]


def test_every_tool_counts_the_same_errors():
    reference, hypothesis = "Клен 15, перевірка звʼязку, прийом", "клин 15 перевірка зв'язку"
    expected = error_rates(reference, hypothesis)
    assert expected["word_errors"] == 2 and expected["reference_words"] == 5
    assert recordings.scores(reference, hypothesis)["word_errors"] == expected["word_errors"]
    assert text_replay.distance(text_replay.words(hypothesis), text_replay.words(reference)) == 2
    assert asr_lab.score_text(reference, hypothesis)["word_errors"] == 2


def test_metric_does_not_apply_text_aliases():
    assert metric_words("зла та") == ["зла", "та"], "алиаси — частина обробки, а не метрики"
