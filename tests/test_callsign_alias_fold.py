import pytest

import callsigns as module
from callsigns import Callsigns


@pytest.mark.parametrize(
    ("name", "number"),
    [
        ("ліра", "68"),
        ("їжак", "11"),
        ("єгер", "22"),
        ("сіль", "47"),
    ],
)
def test_alias_with_folded_characters(
    monkeypatch, name, number
):
    alias = "тестпозивний"

    monkeypatch.setitem(
        module.CALLSIGN_ALIASES,
        alias,
        name,
    )

    dictionary = Callsigns.from_lines([
        f"{name}: {number}"
    ])

    found = dictionary.find(
        f"{alias} {number}"
    )

    assert found == [(name, number, 100)]


def test_existing_ulybka_alias():
    dictionary = Callsigns.from_lines([
        "липка: 700"
    ])

    assert dictionary.find("улыбка 700") == [
        ("липка", "700", 100)
    ]
