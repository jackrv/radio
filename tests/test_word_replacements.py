from core.word_replacements import apply_word_rules


def test_replaces_words_in_different_phrases():
    rules = [
        ("калиниця", "калина"),
        ("на бат", "набат"),
    ]

    assert apply_word_rules(
        "Калиниця 700, я на бат 110",
        rules,
    ) == "Калина 700, я набат 110"


def test_does_not_replace_without_number_context():
    assert apply_word_rules(
        "Калиниця хорошая",
        [("калиниця", "калина")],
    ) == "Калиниця хорошая"


def test_does_not_replace_inside_another_word():
    assert apply_word_rules(
        "пкалиниця 700",
        [("калиниця", "калина")],
    ) == "пкалиниця 700"


def test_numbers_are_never_replaced():
    assert apply_word_rules(
        "На бат 110",
        [
            ("на бат", "набат"),
            ("110", "112"),
        ],
    ) == "Набат 110"


def test_longest_phrase_has_priority():
    assert apply_word_rules(
        "на бат 110",
        [
            ("бат", "друг"),
            ("на бат", "набат"),
        ],
    ) == "набат 110"


def test_replacements_do_not_cascade():
    assert apply_word_rules(
        "на бат 110",
        [
            ("на бат", "набат"),
            ("набат", "шквал"),
        ],
    ) == "набат 110"


def test_conflicting_rules_are_ignored():
    assert apply_word_rules(
        "Лина 68",
        [
            ("лина", "ліна"),
            ("лина", "лино"),
        ],
    ) == "Лина 68"


def test_preserves_capitalization():
    assert apply_word_rules(
        "ЛИНА 68, Лина 100.",
        [("лина", "ліна")],
    ) == "ЛІНА 68, Ліна 100."
