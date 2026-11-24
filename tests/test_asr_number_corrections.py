from shared.callsigns import Callsigns, to_items


def test_whisper_number_typo():
    text = (
        "Віра, шістдесят вістим, "
        "тридцать сім, сорок девять, прийом"
    )

    assert to_items(text) == [
        ("word", "віра"),
        ("num", "68"),
        ("num", "37"),
        ("num", "49"),
        ("word", "прийом"),
    ]

    cs = Callsigns.from_lines(["віра: 68"])
    assert cs.find(text) == [("віра", "68", 100.0)]


def test_unknown_word_is_not_always_number():
    assert to_items("Віра, вістим, прийом") == [
        ("word", "віра"),
        ("word", "вістим"),
        ("word", "прийом"),
    ]
