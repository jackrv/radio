"""Код команди, почутий наполовину: «злива 42» при коді 4242 — команда з позначкою."""
from unittest.mock import Mock

from core.callsigns import Callsigns, command_dictionary
from core.analyzer import fmt_spoken
from core.text_analysis import analyze_text

COMMANDS = command_dictionary(["злива 4242", "дощ: 2626 777", "грім"])


def found(text):
    return [(m.name, m.number, m.partial) for m in COMMANDS.matches(text)]


def test_half_of_a_doubled_code_counts_as_the_command_and_is_marked():
    assert found("злива 42, прийом") == [("злива", "4242", True)]
    assert found("злива 42 42") == [("злива", "4242", False)], "повний код — без позначки"
    assert found("Дощ 26. Прийом") == [("дощ", "2626", True)]


def test_only_doubled_codes_and_only_a_confident_word():
    assert found("злива 43") == [] and found("дощ 77") == [], "не половина коду ABAB"
    assert found("злата 42") == [], "слово має впевнено збігатися з командою"
    assert found("злива 4") == [] and found("злива 420") == []


def test_callsign_dictionary_is_not_affected():
    callsigns = Callsigns.from_lines(["клен: 4242"])
    assert callsigns.find("клен 42") == []


def test_header_shows_what_was_actually_heard():
    analysis = analyze_text("Вишня 12, злива 42, прийом", Callsigns.from_lines(["вишня: 12"]), COMMANDS,
                            Callsigns.from_lines([]), set(), Mock())
    assert [(n, num) for n, num, _ in analysis.commands] == [("злива", "4242")]
    assert analysis.partial_commands == {("злива", "4242")}
    assert fmt_spoken(analysis.commands, analysis.partial_commands) == "злива 42 42 (почуто «42»)"
    assert fmt_spoken(analysis.commands) == "злива 42 42"


def test_zero_without_continuation_is_completed_only_for_unique_zero_code():
    from unittest.mock import Mock
    from core.callsigns import Callsigns, command_dictionary
    from core.text_analysis import analyze_text
    cs, watch = Callsigns.from_lines(["клен: 5"]), Callsigns.from_lines([])
    commands = command_dictionary(["буря 0 11", "буря 222", "грім 0 22", "грім 0 33", "град 0", "град 0 44"])
    for raw in ("буряна, прийом", "буря 0, прийом"):
        result = analyze_text(raw, cs, commands, watch, set(), Mock())
        assert result.text == "БУРЯ 0 11, прийом", raw
        assert fmt_spoken(result.commands, result.partial_commands) == "буря 0 11 (почуто «0»)"
    result = analyze_text("буряна 11", cs, commands, watch, set(), Mock())
    assert fmt_spoken(result.commands, result.partial_commands) == "буря 0 11", "почуто повністю — без позначки"
    for raw in ("грім 0, прийом", "грімна, прийом", "град 0, прийом", "буряна 12"):
        result = analyze_text(raw, cs, commands, watch, set(), Mock())
        assert not result.partial_commands, raw          # кілька кодів на нуль / «0» сам код / інші цифри
    assert "11" not in analyze_text("грімна, прийом", cs, commands, watch, set(), Mock()).text
