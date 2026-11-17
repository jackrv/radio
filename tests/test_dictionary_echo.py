"""Перелік назв словника з підказки Whisper: прибирається, справжні позивні й команди лишаються."""

import pytest

from core.callsigns import Callsigns, command_dictionary
from core.analyzer import strip_dictionary_echo
from tests.test_transcriber import make_pcm, make_transcriber

CALLSIGNS = Callsigns.from_lines(["береза: 31 56", "вишня", "клен: 15"])
COMMANDS = command_dictionary(["злива 4242", "грім", "дощ 2727"])
WATCH = Callsigns.from_lines(["злива", "дощ"])
DICTIONARIES = (CALLSIGNS, COMMANDS, WATCH)


def strip(text):
    return strip_dictionary_echo(text, DICTIONARIES)


@pytest.mark.parametrize("text", [
    "береза вишня клен",                                   # самі назви за абеткою
    "42 42 злива береза вишня грім дощ клен",              # «дощ 42 42, злива …» з підказки
    "42 42, злива 13, дощ 4, береза, вишня, грім, клен",   # назви з чужими номерами — теж підказка
])
def test_pure_echo_is_dropped(text):
    assert strip(text) == ("", True)


@pytest.mark.parametrize("text, expected", [
    ("42 42 злива береза вишня грім клен, дякую за перегляд", "дякую за перегляд"),
    ("Береза, вишня, клен, як чуєте, прийом", "як чуєте, прийом"),
    ("Береза 56, береза вишня клен, прийом", "Береза 56, прийом"),      # номер лишається при своїй назві
    ("береза 31, береза, вишня, клен.", "береза 31"),                   # точний збіг — не викидаємо
    ("Дятел 506, береза, вишня, клен", "Дятел 506"),                    # цифри всередині — номер слова
])
def test_echo_is_cut_out_and_the_rest_kept(text, expected):
    assert strip(text) == (expected, True)


@pytest.mark.parametrize("text", [
    "Злива 42 42, дощ 27 27, береза 31, прийом",   # назви з номерами — справжня передача
    "Береза 31, вишня, грім, прийом",              # дві назви без номерів — не перелік
    "клен грім береза",                            # не за абеткою
    "береза 31 вишня клен",                        # «вишня клен» — лише дві
])
def test_real_speech_is_untouched(text):
    assert strip(text) == (text, False)


def test_receiver_drops_echo_before_processing():
    worker, recognizer, process_text, processor = make_transcriber(text="42 42 злива береза вишня грім дощ клен")
    processor.callsigns, processor.commands, processor.watch = DICTIONARIES
    worker.process_segment(make_pcm())
    process_text.assert_not_called()
    processor.process.assert_not_called()


def test_receiver_keeps_the_rest_without_echo():
    worker, recognizer, process_text, processor = make_transcriber(text="Береза 56, береза вишня клен, прийом")
    processor.callsigns, processor.commands, processor.watch = DICTIONARIES
    worker.process_segment(make_pcm())
    process_text.assert_called_once_with("Береза 56, прийом")
