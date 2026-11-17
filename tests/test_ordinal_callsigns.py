"""Порядкові слова як алиаси позивних групи: «другий» — «дятел 2»."""

import pytest

import callsigns
from callsigns import Callsigns
from core.analyzer import fmt, split_roles

CALLSIGNS = Callsigns.from_lines(["дятел: 1 2 3", "береза: 56"])


@pytest.fixture(autouse=True)
def aliases(monkeypatch):
    monkeypatch.setattr(callsigns, "CALLSIGN_ALIASES", {"перший": "дятел", "другий": "дятел", "четвертий": "дятел"})


def found(text):
    return [(name, number) for name, number, _ in CALLSIGNS.find(text)]


def test_ordinal_means_callsign_number():
    assert found("другий, прийняв") == [("дятел", "2")]
    assert found("18 36, перший прийняв, другий прийняв") == [("дятел", "1"), ("дятел", "2")]


def test_only_with_alias_and_allowed_number():
    assert found("третій прийняв") == [], "без алиаса порядкове слово — звичайне слово"
    assert found("четвертий прийняв") == [], "номера 4 у словнику дятла немає"


def test_ordinal_takes_part_in_roles():
    to_found, from_found = split_roles("Береза 56, я другий, прийнято", CALLSIGNS)
    assert (fmt(to_found), fmt(from_found)) == ("береза 56", "дятел 2")
