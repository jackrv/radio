"""«Кому / від кого»: «я», форма «К, (К,) О, текст» і випадки, де ролі не вгадуються."""

import pytest

from callsigns import Callsigns
from core.analyzer import fmt, split_roles

CALLSIGNS = Callsigns.from_lines(["береза: 31 56 700 706", "клен: 15", "вишня"])


def roles(text):
    to_found, from_found = split_roles(text, CALLSIGNS)
    return fmt(to_found), fmt(from_found)


@pytest.mark.parametrize("text, expected", [
    ("Береза 700, Береза 706, прийняв прийом", ("береза 700", "береза 706")),
    ("Береза 700, береза 700, береза 706, прийнято, прийом", ("береза 700", "береза 706")),
    ("Вишня, клен 15, прийом", ("вишня", "клен 15")),
])
def test_two_callsigns_in_a_row_are_recipient_then_sender(text, expected):
    assert roles(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("Береза 31, Береза 56, Клен 15, 17 76, прийом", ("береза 31, береза 56, клен 15", "")),  # кілька адресатів
    ("Береза 31, Береза 56, 17 76", ("береза 31, береза 56", "")),       # розсилка з цифрами
    ("береза 56 посилку отримав клен 15 посилку отримав", ("береза 56, клен 15", "")),  # злиті передачі
    ("Береза 31, вишня, прийом", ("береза 31, вишня", "")),             # без номера — можлива вигадка Whisper
    ("Береза 31, прийнято, прийом", ("береза 31", "")),
])
def test_roles_are_not_guessed_outside_that_form(text, expected):
    assert roles(text) == expected


def test_self_marker_keeps_number_groups_apart():
    assert roles("Береза 56, береза 56, я клен 15, 39 64, прийом") == ("береза 56", "клен 15")
    assert roles("Береза 31, Я Клен, 15, прийом") == ("береза 31", "клен 15")
    assert roles("Береза 31, ясно, прийом") == ("береза 31", ""), "«я» лише окремим словом"


@pytest.mark.parametrize("text, expected", [
    ("Береза 31, 53 16, прийом", ("", "береза 31")),             # один раз і одразу цифри
    ("Береза 31, 5316, приєм", ("", "береза 31")),
    ("вишня, 63 27, прийом", ("", "вишня")),
    ("береза 56 посилку отримав прийом", ("", "береза 56")),
    ("Клен 15, прийняв", ("", "клен 15")),
])
def test_single_callsign_sender_forms(text, expected):
    assert roles(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("Береза 31, береза 31, 53 19, прийом", ("береза 31", "")),   # двічі — адресат за протоколом
    ("Береза 31, прийнято, прийом", ("береза 31", "")),
    ("Береза 31, на звʼязку", ("береза 31", "")),
    ("посилку отримав, береза 31", ("береза 31", "")),            # позивний не на початку
])
def test_single_callsign_stays_recipient(text, expected):
    assert roles(text) == expected
