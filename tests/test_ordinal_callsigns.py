"""Порядкові слова як алиаси позивних групи: «другий» — «дятел 2»."""

import pytest

import core.callsigns as callsigns
from core.callsigns import Callsigns
from core.analyzer import fmt, split_roles

CALLSIGNS = Callsigns.from_lines(["дятел: 1 2 3", "береза: 56"])


@pytest.fixture(autouse=True)
def aliases(monkeypatch):
    monkeypatch.setattr(callsigns, "CALLSIGN_ALIASES", {"перший": "дятел", "другий": "дятел", "четвертий": "дятел"})


def found(text):
    return [(name, number) for name, number, _ in CALLSIGNS.find(text)]


def test_ordinal_means_callsign_number():
    assert found("другий, прийняв") == [("дятел", "2")]
    assert found("31 05, перший прийняв, другий прийняв") == [("дятел", "1"), ("дятел", "2")]


def test_only_with_alias_and_allowed_number():
    assert found("третій прийняв") == [], "без алиаса порядкове слово — звичайне слово"
    assert found("четвертий прийняв") == [], "номера 4 у словнику дятла немає"


def test_ordinal_takes_part_in_roles():
    to_found, from_found = split_roles("Береза 56, я другий, прийнято", CALLSIGNS)
    assert (fmt(to_found), fmt(from_found)) == ("береза 56", "дятел 2")


def test_number_after_ordinal_is_not_its_callsign_number():
    """«Другий, 18» — це дятел 2 і окреме число 18, а не «дятел 18»."""
    from core.template_text import template_text
    from services.text_check import check_text
    assert template_text("Другий, 18, прийом", CALLSIGNS) == "Другий, 18, прийом"
    issues = check_text("Другий, 18, прийом", CALLSIGNS)["issues"]
    assert not [i for i in issues if "дятел" in i["message"].lower()], issues
    assert found("Другий, 18, прийом") == [("дятел", "2")]
