from core.word_replacements import apply_word_rules


def test_replaces_words_in_different_phrases():
    rules = [
        ("улыбка", "липка"),
        ("на ган", "наган"),
    ]

    assert apply_word_rules(
        "Улыбка 700, я на ган 110",
        rules,
    ) == "Липка 700, я наган 110"


def test_does_not_replace_without_number_context():
    assert apply_word_rules(
        "Улыбка хорошая",
        [("улыбка", "липка")],
    ) == "Улыбка хорошая"


def test_does_not_replace_inside_another_word():
    assert apply_word_rules(
        "пулыбка 700",
        [("улыбка", "липка")],
    ) == "пулыбка 700"


def test_numbers_are_never_replaced():
    assert apply_word_rules(
        "На ган 110",
        [
            ("на ган", "наган"),
            ("110", "112"),
        ],
    ) == "Наган 110"


def test_longest_phrase_has_priority():
    assert apply_word_rules(
        "на ган 110",
        [
            ("ган", "друг"),
            ("на ган", "наган"),
        ],
    ) == "наган 110"


def test_replacements_do_not_cascade():
    assert apply_word_rules(
        "на ган 110",
        [
            ("на ган", "наган"),
            ("наган", "шторм"),
        ],
    ) == "наган 110"


def test_conflicting_rules_are_ignored():
    assert apply_word_rules(
        "Лира 68",
        [
            ("лира", "ліра"),
            ("лира", "лиро"),
        ],
    ) == "Лира 68"


def test_preserves_capitalization():
    assert apply_word_rules(
        "ЛИРА 68, Лира 100.",
        [("лира", "ліра")],
    ) == "ЛІРА 68, Ліра 100."
