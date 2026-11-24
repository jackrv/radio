import re
from unittest.mock import Mock

import pytest

from shared.callsigns import Callsigns,command_dictionary
from shared.text_metrics import metric_words
from shared.transcript_format import tidy_text,infer_transmission_boundaries

CS=Callsigns.from_lines(['клен: 12 34','дуб: 56','оператор: *'])
COMMANDS=command_dictionary([])


def test_commands_uppercase_and_remove_commas_without_inventing_missing_digits():
    commands = command_dictionary(['грім 022', 'злива 7373'])
    text = 'грім, 0, 22, злива, 73, 73, прийом'
    assert tidy_text(text, CS, commands) == 'ГРІМ 0 22, ЗЛИВА 73 73, прийом'
    assert tidy_text('грім, 13, прийом', CS, commands) == 'ГРІМ 13, прийом'
    assert tidy_text('далі злива над містом', CS, commands) == 'Далі злива над містом'


@pytest.mark.parametrize('text,expected',[
    ('клен 12, клен 34, до роботи приступив, прийом, клен 34, прийнято, прийом',
     'Клен 12, Клен 34, до роботи приступив, прийом / Клен 34, прийнято, прийом'),
    ('клен 12, я дуб 56, перевірка звʼязку, прийом, дуб 56, чую добре, прийом',
     'Клен 12, я Дуб 56, перевірка звʼязку, прийом / Дуб 56, чую добре, прийом'),
])
def test_shared_formatter_infers_only_structurally_complete_exchanges(text,expected):
    result=tidy_text(text,CS,COMMANDS)
    assert result==expected
    assert tidy_text(result,CS,COMMANDS)==result
    assert metric_words(text)==metric_words(result)
    assert re.findall(r'\d+',text)==re.findall(r'\d+',result)


@pytest.mark.parametrize('text',[
    'клен 12, перевірте прийом сигналу, прийом',
    'клен 12, прийом, клен 12, прийом',
    'клен 12, прийом, 34, прийнято, прийом',
    'клен 12, прийом, незнайомий 34, прийнято, прийом',
    'клен 12, прийом, клен 34, прийнято',
    '«клен 12, прийом, клен 34, прийнято, прийом»',
])
def test_over_alone_never_proves_another_transmission(text):
    assert infer_transmission_boundaries(text,CS)==text


@pytest.mark.parametrize('text',['у 2026 році','дата 20/11/2026','час 12:30','код1234','дріб 12.34'])
def test_protected_non_radio_numbers_are_not_reformatted(text):
    assert tidy_text(text,CS,COMMANDS)==text


def test_address_forms_are_capitalized_without_changing_declension():
    assert tidy_text('клен 12, оператору 34, до роботи приступив, прийом',CS,COMMANDS)==\
        'Клен 12, Оператору 34, до роботи приступив, прийом'


def test_live_and_editor_use_the_same_formatter():
    from shared.text_analysis import analyze_text
    from shared.text_check import tidy_text as editor_formatter
    assert editor_formatter is tidy_text
    text='клен 12, до роботи приступив, прийом, дуб 56, прийнято, прийом'
    result=analyze_text(text,CS,COMMANDS,Callsigns.from_lines([]),set(),Mock())
    assert result.text==tidy_text(text,CS,COMMANDS)


def test_formatting_never_changes_recipient_metadata_or_rejection(monkeypatch):
    import shared.text_analysis as analysis
    watch = Callsigns.from_lines([])
    text = 'клен 12, до роботи приступив, прийом, дуб 56, прийнято, прийом'
    formatted = analysis.analyze_text(text, CS, COMMANDS, watch, set(), Mock())
    monkeypatch.setattr(analysis, 'tidy_text', lambda value, *_: value)
    original = analysis.analyze_text(text, CS, COMMANDS, watch, set(), Mock())
    for field in ('callsigns', 'commands', 'watch', 'recipient', 'sender', 'rejected', 'partial_commands'):
        assert getattr(formatted, field) == getattr(original, field)
