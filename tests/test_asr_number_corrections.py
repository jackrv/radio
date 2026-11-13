from callsigns import Callsigns, to_items


def test_whisper_number_typo():
    text = (
        "Ліра, шістдесят вістим, "
        "тридцать сім, сорок девять, прийом"
    )

    assert to_items(text) == [
        ("word", "ліра"),
        ("num", "68"),
        ("num", "37"),
        ("num", "49"),
        ("word", "прийом"),
    ]

    cs = Callsigns.from_lines(["ліра: 68"])
    assert cs.find(text) == [("ліра", "68", 100.0)]


def test_unknown_word_is_not_always_number():
    assert to_items("Ліра, вістим, прийом") == [
        ("word", "ліра"),
        ("word", "вістим"),
        ("word", "прийом"),
    ]
