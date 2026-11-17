import pytest

import core.callsigns as module
from core.callsigns import Callsigns


@pytest.mark.parametrize(
    ("name", "number"),
    [
        ("ліна", "68"),
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
        "калина: 700"
    ])

    assert dictionary.find("калиниця 700") == [
        ("калина", "700", 100)
    ]


def test_alias_reaches_callsign_without_number(monkeypatch):
    monkeypatch.setitem(module.CALLSIGN_ALIASES, "дубок", "дуб")
    dictionary = Callsigns.from_lines(["дуб", "клен: 300"])
    assert dictionary.find("дубок, прийнято") == [("дуб", None, 100)]
    assert dictionary.find("клен 300") == [("клен", "300", 100)]
