from core.word_learning import analyze_word_changes


def test_two_word_corrections():
    assert analyze_word_changes(
        "Вішня 700, я ду бок 110, принял.",
        "Вишня 700, я Дубок 110, принял.",
    ).pairs == [
        ("вішня", "вишня"),
        ("ду бок", "дубок"),
    ]


def test_ukrainian_letters():
    assert analyze_word_changes(
        "Вира 68",
        "Віра 68",
    ).pairs == [("вира", "віра")]


def test_different_numbers_are_rejected():
    assert analyze_word_changes(
        "Вішня 700",
        "Вишня 720",
    ).pairs == []


def test_number_words_are_not_learned():
    assert analyze_word_changes(
        "Дубок сто",
        "Дубок 100",
    ).pairs == []


def test_insertions_and_deletions_are_ignored():
    assert analyze_word_changes(
        "Вишня 700",
        "Вишня новый 700",
    ).pairs == []

    assert analyze_word_changes(
        "Вишня новый 700",
        "Вишня 700",
    ).pairs == []


def test_long_rewrites_are_ignored():
    assert analyze_word_changes(
        "нам не понятно",
        "все ясно сейчас",
    ).pairs == []


def test_case_and_punctuation_are_not_rules():
    assert analyze_word_changes(
        "Вишня, 700!",
        "вишня 700",
    ).pairs == []


def test_empty_input():
    assert analyze_word_changes("", "Вишня").pairs == []
    assert analyze_word_changes("Вишня", "").pairs == []
