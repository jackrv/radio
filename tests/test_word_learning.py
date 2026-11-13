from core.word_learning import extract_word_changes


def test_two_word_corrections():
    assert extract_word_changes(
        "Улыбка 700, я на ган 110, принял.",
        "Липка 700, я Наган 110, принял.",
    ) == [
        ("улыбка", "липка"),
        ("на ган", "наган"),
    ]


def test_ukrainian_letters():
    assert extract_word_changes(
        "Лира 68",
        "Ліра 68",
    ) == [("лира", "ліра")]


def test_different_numbers_are_rejected():
    assert extract_word_changes(
        "Улыбка 700",
        "Липка 720",
    ) == []


def test_number_words_are_not_learned():
    assert extract_word_changes(
        "Наган сто",
        "Наган 100",
    ) == []


def test_insertions_and_deletions_are_ignored():
    assert extract_word_changes(
        "Липка 700",
        "Липка новый 700",
    ) == []

    assert extract_word_changes(
        "Липка новый 700",
        "Липка 700",
    ) == []


def test_long_rewrites_are_ignored():
    assert extract_word_changes(
        "нам не понятно",
        "все ясно сейчас",
    ) == []


def test_case_and_punctuation_are_not_rules():
    assert extract_word_changes(
        "Липка, 700!",
        "липка 700",
    ) == []


def test_empty_input():
    assert extract_word_changes("", "Липка") == []
    assert extract_word_changes("Липка", "") == []
