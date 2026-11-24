from unittest.mock import Mock

import pytest

from shared.callsigns import Callsigns, command_dictionary
from shared.command_completion import restore_repeated_command_codes
from shared.transcript_format import tidy_text
from shared.text_check import check_text

CS = Callsigns.from_lines(['клен: 12'])


@pytest.mark.parametrize('line', ['злива 4242', 'злива 42 42'])
def test_both_dictionary_spellings_restore_one_missing_repeat(line):
    commands = command_dictionary([line])
    assert commands.numbers['злива'] == {'4242'}
    text = 'клен 12, злива, 42, прийом'
    result = tidy_text(text, CS, commands, restore_codes=True)
    assert result == 'Клен 12, ЗЛИВА 42 42, прийом'
    assert tidy_text(result, CS, commands) == result
    checked = check_text(text, CS, commands)
    assert checked['formatted'] == result
    assert any('повтору коду' in i['message'] for i in checked['issues'])
    assert not any('номера 42 немає' in i['message'] for i in checked['issues'])


@pytest.mark.parametrize('text', [
    'злива 43', 'злива 4', 'злива 424', 'злива 42 43', 'злива 42 42',
    'злива42', 'злива 42x', 'злива 42_код', 'злівa 42',
    'код_злива 42', '«злива 42»', 'злива 42.5', 'злива 42/50',
])
def test_no_repair_for_other_numbers_full_codes_typos_or_embedded_text(text):
    assert restore_repeated_command_codes(text, CS, command_dictionary(['злива 4242'])) == text


@pytest.mark.parametrize('lines', [
    ['злива: 42 4242'], ['злива: 4242 4255'], ['злива: * 4242'], ['злива: 42 42'],
])
def test_dictionary_ambiguity_is_not_a_license_to_expand(lines):
    assert restore_repeated_command_codes('злива 42', CS, command_dictionary(lines)) == 'злива 42'


def test_command_and_callsign_collision_preserves_number():
    cs = Callsigns.from_lines(['злива: 42'])
    assert restore_repeated_command_codes('злива 42', cs, command_dictionary(['злива 4242'])) == 'злива 42'


@pytest.mark.parametrize('line', ['злива 4242', 'злива 42 42'])
def test_default_live_event_restores_repeat_and_keeps_original_and_partial_marker(line):
    from tests.test_notification_text import processor
    bot, save = Mock(), Mock(return_value=7)
    original = 'злива, 42, прийом'
    processor(bot, commands=command_dictionary([line]), save=save).process(original, b'\0\0')
    assert save.call_args.kwargs['raw_text'] == original
    assert save.call_args.args[4] == 'ЗЛИВА 42 42, прийом'
    assert bot.notify.call_args.args[3] == 'ЗЛИВА 42 42, прийом'
    assert 'почуто «42»' in bot.notify.call_args.kwargs['commands_str']
