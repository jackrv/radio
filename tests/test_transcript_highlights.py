from core.callsigns import Callsigns, command_dictionary
from services.text_check import highlight_spans, check_text

CS = Callsigns.from_lines(['клен: 12'])
COMMANDS = command_dictionary(['грім 022'])


def test_distinct_kinds_and_problem_spans_use_utf16_positions():
    text = '📻 Клен 12 / грім 0 22, клєн 12'
    issues = check_text(text, CS, COMMANDS)['issues']
    spans = highlight_spans(text, CS, COMMANDS, issues)
    def contents(span):
        raw = text.encode('utf-16-le')
        return raw[span['start']*2:span['end']*2].decode('utf-16-le')
    assert ('callsign', 'Клен') in [(s['kind'],contents(s)) for s in spans]
    assert ('command', 'грім') in [(s['kind'],contents(s)) for s in spans]
    assert ('boundary', '/') in [(s['kind'],contents(s)) for s in spans]
    assert ('problem', 'клєн') in [(s['kind'],contents(s)) for s in spans]
    assert not any(s['kind']=='command' for s in highlight_spans('грім над містом', CS, COMMANDS))


def test_unknown_callsign_is_marked_without_suggesting_a_substitution():
    text = 'Листок 13, прийом'
    issues = check_text(text, CS, COMMANDS)['issues']
    spans = highlight_spans(text, CS, COMMANDS, issues)
    assert any(s['kind']=='problem' and text[s['start']:s['end']]=='Листок' for s in spans)
