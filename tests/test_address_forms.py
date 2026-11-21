"""Форми звертання: синтетичні назви, без робочого словника й моделей."""
from unittest.mock import Mock

import pytest

from core.callsigns import Callsigns, command_dictionary
from core.analyzer import canonical_text
from core.template_text import template_text
from core.text_analysis import analyze_text
from services.text_check import check_text
from core.text_metrics import metric_words


@pytest.mark.parametrize('form', ['Клену', 'клену', 'КЛЕНУ', 'Кленові'])
@pytest.mark.parametrize('tail', ['56, прийом', '42 42, посилку отримав', 'прийом'])
def test_address_form_is_preserved_in_live_and_editor_corrections(form, tail):
    callsigns = Callsigns.from_lines(['клен: 56', 'вишня'])
    commands = command_dictionary(['дощ 4242'])
    watch = Callsigns.from_lines([])
    text = f'{form} {tail}'
    assert canonical_text(text, (commands, callsigns, watch)) == text
    assert template_text(text, callsigns, commands.names) == text
    live = analyze_text(text, callsigns, commands, watch, set(), Mock()).text
    assert metric_words(live) == metric_words(text)
    assert live.startswith(form.capitalize())
    result = check_text(text, callsigns, commands, watch)
    assert form.casefold() in (result['formatted'] or text).casefold()
    issues = result['issues']
    assert not any(i['kind'] == 'spelling' and i.get('fixed_text', text) != text for i in issues)


def test_same_protection_applies_to_a_standalone_dictionary_name():
    callsigns = Callsigns.from_lines(['клен'])
    assert template_text('Клену 56, прийом', callsigns) == 'Клену 56, прийом'


def test_real_misspelling_still_has_a_suggestion_and_numbers_are_preserved():
    callsigns = Callsigns.from_lines(['береза: 56'])
    assert template_text('Бериза 56, прийом', callsigns) == 'Береза 56, прийом'
    assert any(i['kind'] == 'spelling' for i in check_text('Бериза 56, прийом', callsigns)['issues'])
