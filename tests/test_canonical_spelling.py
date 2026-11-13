from callsigns import Callsigns
from core.analyzer import correction_is_safe


def test_preserve_ukrainian_callsign_spelling():
    dictionary = Callsigns.from_lines([
        "ліра: 68",
        "липка: 700",
    ])

    dictionaries = (dictionary,)

    # Нельзя портить правильное украинское написание.
    assert not correction_is_safe(
        "Ліра 68",
        "лира 68",
        dictionaries,
    )

    # Исправление неправильного написания разрешено.
    assert correction_is_safe(
        "лира 68",
        "Ліра 68",
        dictionaries,
    )

    # Qwen не должна добавлять слова, которых не было в эфире.
    assert not correction_is_safe(
        "Ліра 68, перевірка",
        "Ліра 68, перевірка зв'язку",
        dictionaries,
    )
