from callsigns import Callsigns
from core.analyzer import correction_is_safe


def test_preserve_ukrainian_callsign_spelling():
    dictionary = Callsigns.from_lines([
        "віра: 68",
        "вишня: 700",
    ])

    dictionaries = (dictionary,)

    # Нельзя портить правильное украинское написание.
    assert not correction_is_safe(
        "Віра 68",
        "вира 68",
        dictionaries,
    )

    # Исправление неправильного написания разрешено.
    assert correction_is_safe(
        "вира 68",
        "Віра 68",
        dictionaries,
    )

    # Qwen не должна добавлять слова, которых не было в эфире.
    assert not correction_is_safe(
        "Віра 68, перевірка",
        "Віра 68, перевірка зв'язку",
        dictionaries,
    )
