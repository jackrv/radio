from callsigns import Callsigns, to_items


def test_separate_numbers_after_callsign():
    text = "Ліра 68, 37, 49, прийом"

    assert to_items(text) == [
        ("word", "ліра"),
        ("num", "68"),
        ("num", "37"),
        ("num", "49"),
        ("word", "прийом"),
    ]

    cs = Callsigns.from_lines(["ліра: 68"])
    assert cs.find(text) == [("ліра", "68", 100.0)]


def test_separate_combined_number():
    text = "Ліра 68, 3749, прийом"

    assert to_items(text) == [
        ("word", "ліра"),
        ("num", "68"),
        ("num", "3749"),
        ("word", "прийом"),
    ]

    cs = Callsigns.from_lines(["ліра: 68"])
    assert cs.find(text) == [("ліра", "68", 100.0)]


def test_spoken_numbers_and_comma():
    text = (
        "Ліра шістдесят вісім, "
        "тридцять сім, сорок дев'ять, прийом"
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


def test_spoken_digits_still_combine():
    assert to_items("Ліра шість вісім") == [
        ("word", "ліра"),
        ("num", "68"),
    ]


def test_comma_between_name_and_number():
    cs = Callsigns.from_lines(["ліра: 68"])

    assert cs.find("Ліра, 68") == [
        ("ліра", "68", 100.0)
    ]
