from shared.callsigns import Callsigns
from shared.analyzer import correction_is_safe


def dictionaries():
    return (
        Callsigns.from_lines(["вишня: 700 720"]),
        Callsigns.from_lines([]),
        Callsigns.from_lines([]),
    )


def test_rejects_added_phrase():
    assert not correction_is_safe(
        "Вишня 700",
        "Вишня 700, я на связи",
        dictionaries(),
    )


def test_rejects_single_added_word():
    assert not correction_is_safe(
        "Вишня 700",
        "Вишня 700 прийом",
        dictionaries(),
    )


def test_rejects_deleted_words():
    assert not correction_is_safe(
        "Вишня 700, я на связи",
        "Вишня 700",
        dictionaries(),
    )


def test_allows_spelling_correction():
    assert correction_is_safe(
        "Вишня 700, я на свези",
        "Вишня 700, я на зв'язку",
        dictionaries(),
    )
