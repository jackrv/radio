from types import SimpleNamespace

from core.word_rule_validation import (
    filter_dictionary_word_rules,
)


def make_dictionaries():
    return (
        SimpleNamespace(
            names=["калина", "набат", "ліна"]
        ),
        SimpleNamespace(names=["прийом"]),
        SimpleNamespace(names=[]),
    )


def test_valid_callsign_corrections():
    rules = [
        ("калиниця", "калина"),
        ("на бат", "набат"),
        ("лина", "ліна"),
    ]

    assert filter_dictionary_word_rules(
        rules, make_dictionaries()
    ) == rules


def test_unknown_target_is_rejected():
    assert filter_dictionary_word_rules(
        [("неизвестное", "случайное")],
        make_dictionaries(),
    ) == []


def test_existing_callsign_is_protected():
    assert filter_dictionary_word_rules(
        [("калина", "набат")],
        make_dictionaries(),
    ) == []


def test_numbers_cannot_be_rules():
    assert filter_dictionary_word_rules(
        [
            ("110", "112"),
            ("на бат", "набат"),
        ],
        make_dictionaries(),
    ) == [
        ("на бат", "набат")
    ]


def test_empty_dictionaries_reject_rules():
    assert filter_dictionary_word_rules(
        [("калиниця", "калина")],
        (SimpleNamespace(names=[]),),
    ) == []
