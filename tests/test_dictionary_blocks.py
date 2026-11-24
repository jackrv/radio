"""Словникові блоки без моделей, робочої бази та приватних назв."""
from unittest.mock import Mock

import pytest

from shared.callsigns import Callsigns,command_dictionary
from shared.dictionary_blocks import constrain_dictionary_text
from shared.text_analysis import analyze_text
from shared.transcript_format import tidy_text
from shared.dictionary_number_evidence import build_rules
from shared.text_replay import templates
from shared.text_check import check_text


def dictionaries():
    return Callsigns.from_lines(['клен: 5 600 609','дуб: 12']),command_dictionary(['грім 0 22','грім 333','злива 42 42'])


@pytest.mark.parametrize('raw,expected',[
    ('грім 023','ГРІМ 0 22'),('грім 0, 23','ГРІМ 0 22'),('грім 022','ГРІМ 0 22'),
    ('грім 333','ГРІМ 333'),('злива 41 42','ЗЛИВА 42 42'),('злива, 42 42','ЗЛИВА 42 42'),
    ("клен, п'ять, прийом",'Клен 5, прийом'),('клен шістсот, прийом','Клен 600, прийом'),
])
def test_exact_dictionary_blocks_and_spoken_numbers(raw,expected):
    cs,commands=dictionaries()
    assert tidy_text(raw,cs,commands)==expected
    assert tidy_text(expected,cs,commands)==expected


@pytest.mark.parametrize('raw',[
    'грім 123','злива 41 43','злива 42421','код_грім 023','грім 023abc',
    '«грім 023»','"злива 41 42"','грім 0.23','клен 800','я дуб 13','у 2026 році',
])
def test_not_a_nearest_number_replacement(raw):
    cs,commands=dictionaries()
    assert constrain_dictionary_text(raw,cs,commands)[0]==raw


def test_ambiguous_command_and_valid_other_code_are_preserved():
    cs,_=dictionaries()
    commands=command_dictionary(['злива 42 42','злива 33 43'])
    assert constrain_dictionary_text('злива 42 43',cs,commands)[0]=='злива 42 43'
    commands=command_dictionary(['злива 42 42','злива 42 43'])
    assert tidy_text('злива 42 43',cs,commands)=='ЗЛИВА 42 43'
    assert constrain_dictionary_text('злива 41 42',cs,command_dictionary(['злива: * 4242']))[0]=='злива 41 42'
    assert constrain_dictionary_text('злива 41 42',Callsigns.from_lines(['злива: 4142']),commands)[0]=='злива 41 42'


def feedback(source,raw='клен 800',reference='Клен 600'):
    return dict(id=str(source),source='db:'+str(source),raw_text=raw,reference=reference)


def test_number_rules_need_three_independent_unanimous_sources_and_current_dictionary():
    cs,commands=dictionaries()
    assert build_rules([feedback(1),feedback(2)],cs,commands)=={}
    assert build_rules([feedback(1)]*3,cs,commands)=={}
    rows=[feedback(i) for i in range(3)]
    rules=build_rules(rows,cs,commands)
    assert rules=={('клен','800'):{'target':'600','sources':3}}
    assert build_rules(rows+[feedback(4,reference='Клен 609')],cs,commands)=={}
    assert build_rules(rows+[feedback(4,reference='Клен 800')],cs,commands)=={}
    assert build_rules(rows,Callsigns.from_lines(['клен: 600 800']),commands)=={}
    assert build_rules(rows,Callsigns.from_lines(['клен: 609']),commands)=={}
    assert build_rules(rows,Callsigns.from_lines(['клен: *']),commands)=={}
    assert build_rules([feedback(i,raw='клен 800, клен 800') for i in range(3)],cs,commands)=={}


def test_alias_and_heard_self_prefix_use_same_evidence_without_inventing_self():
    cs,commands=dictionaries()
    with templates(aliases=({}, {'клин':'клен'})):
        rows=[feedback(1,'клин 800'),feedback(2,'яклин 800','я Клен 600'),feedback(3,'Клен 800')]
        cs.number_rules=build_rules(rows,cs,commands)
        assert tidy_text('клин 800, прийом',cs,commands)=='Клен 600, прийом'
        assert tidy_text('яклин 800, прийом',cs,commands)=='Я Клен 600, прийом'
        assert constrain_dictionary_text('дуб 800',cs,commands)[0]=='дуб 800'
        assert constrain_dictionary_text('число 800',cs,commands)[0]=='число 800'
        assert constrain_dictionary_text('клен 800abc',cs,commands)[0]=='клен 800abc'
        assert constrain_dictionary_text('«клин 800»',cs,commands)[0]=='«клин 800»'


def test_rule_cannot_override_newly_valid_source_or_removed_target():
    cs,commands=dictionaries();cs.number_rules={('клен','800'):{'target':'600','sources':3}}
    cs.numbers['клен'].add('800')
    assert constrain_dictionary_text('клен 800',cs,commands)[0]=='клен 800'
    cs.numbers['клен'].remove('800');cs.numbers['клен'].remove('600')
    assert constrain_dictionary_text('клен 800',cs,commands)[0]=='клен 800'


def test_corrected_blocks_drive_metadata_and_editor_with_unchanged_raw():
    from tests.test_notification_text import processor
    cs,commands=dictionaries();cs.number_rules={('клен','800'):{'target':'600','sources':3}}
    raw="грім 023, грім 023, я клен 800, злива 41 42, прийом"
    result=analyze_text(raw,cs,commands,Callsigns.from_lines([]),set(),Mock())
    assert ('грім','022',100.0) in result.commands
    assert result.sender[0][:2]==('клен','600')
    checked=check_text(raw,cs,commands)
    assert checked['formatted']==result.text
    assert any('3 незалежними' in issue['message'] for issue in checked['issues'])
    assert not any('номера 023 немає' in issue['message'] for issue in checked['issues'])
    bot,save=Mock(),Mock(return_value=1)
    event=processor(bot,commands=commands,save=save);event.callsigns=cs
    event.process(raw,b'\0\0',raw_text=raw)
    assert save.call_args.kwargs['raw_text']==raw
    assert save.call_args.args[4]==result.text


def make_archive(path, rows):
    import json
    import sqlite3
    path.mkdir(exist_ok=True)
    with sqlite3.connect(path/'index.sqlite3') as db:
        db.execute('CREATE TABLE recordings (id TEXT PRIMARY KEY, created REAL, label TEXT, reference TEXT, data TEXT, reviewed_at REAL)')
        for i,row in enumerate(rows):
            db.execute('INSERT INTO recordings VALUES (?,?,?,?,?,?)',
                       (row['id'],row.get('created',i),'speech',row['reference'],
                        json.dumps({'raw_text':row['raw_text'],'fix_id':row.get('source')}),i))


def test_readonly_archive_rules_restart_cache_refresh_and_independent_sources(tmp_path,monkeypatch):
    import sqlite3
    from shared import dictionary_number_evidence as evidence
    cs,commands=dictionaries()
    rows=[feedback(i) for i in range(3)]
    make_archive(tmp_path,rows)
    expected={('клен','800'):{'target':'600','sources':3}}
    assert evidence.load_rules(tmp_path,cs,commands)==expected
    evidence._cache.clear()  # Перезапуск не втрачає підтверджені джерела.
    assert evidence.load_rules(tmp_path,cs,commands)==expected
    assert evidence.load_rules(tmp_path,cs,commands,allowed_ids={'0','1'})=={}
    # Повторне редагування джерела змінює еталон, а не додає голос.
    with sqlite3.connect(tmp_path/'index.sqlite3') as db:
        db.execute("UPDATE recordings SET reference='Клен 609' WHERE id='2'")
    monkeypatch.setattr(evidence,'REFRESH_S',0)
    assert evidence.load_rules(tmp_path,cs,commands)=={}
    with sqlite3.connect(tmp_path/'index.sqlite3') as db:
        assert db.execute('SELECT COUNT(*) FROM recordings').fetchone()[0]==3


def test_unreadable_archive_disables_number_changes(tmp_path,caplog):
    from shared.dictionary_number_evidence import load_rules
    cs,commands=dictionaries()
    assert load_rules(tmp_path,cs,commands)=={}
    (tmp_path/'index.sqlite3').write_text('not sqlite')
    assert load_rules(tmp_path,cs,commands)=={}
    assert 'DatabaseError' in caplog.text
    assert '800' not in caplog.text


def test_editor_loads_evidence_from_explicit_archive_without_cross_database_leak(tmp_path):
    import sqlite3
    from shared.dictionary_store import init_db
    from shared.text_check import check_with_db
    from shared.text_replay import load_setup
    archive=tmp_path/'archive'
    make_archive(archive,[feedback(i) for i in range(3)])
    db_path=tmp_path/'events.db'
    init_db(db_path)
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO dictionary_entries (dictionary_type,line_number,raw_line) VALUES ('callsigns',1,'клен: 5 600 609')")
        assert check_with_db(db,'клен 800, прийом',archive=archive)['formatted']=='Клен 600, прийом'
        assert check_with_db(db,'клен 800, прийом',archive=tmp_path/'empty')['formatted']=='Клен 800, прийом'
    setup,_,_=load_setup(db_path,archive)
    assert setup[0].number_rules[('клен','800')]['target']=='600'


def test_whisper_codes_prompt_is_opt_in_and_uses_dictionary_grouping():
    from shared.dictionary_loader import build_prompt
    from shared.lab_queue import PRESETS
    cs,commands=dictionaries();watch=Callsigns.from_lines([])
    default=build_prompt(cs,commands,watch)
    assert not any(char.isdigit() for char in default)
    prompt=build_prompt(cs,commands,watch,include_command_codes=True)
    assert 'грім 0 22' in prompt and 'злива 42 42' in prompt and prompt.endswith(default)
    args=PRESETS['dictionary-blocks']['args']
    assert args[args.index('--prompt')+1]=='receiver-command-codes'
    assert next(iter(PRESETS))=='base'


def test_lab_cannot_learn_callsign_number_from_holdout_references(tmp_path):
    import sqlite3
    from shared.dictionary_store import init_db
    from lab.asr_lab import dictionaries_from,split_of
    times={kind:[i*1200 for i in range(50) if split_of(i*1200)==kind] for kind in ('tune','holdout')}
    rows=[dict(feedback(i),created=times['tune'][i]) for i in range(2)]
    rows.append(dict(feedback(2),created=times['holdout'][0]))
    archive=tmp_path/'archive';make_archive(archive,rows)
    db_path=tmp_path/'events.db';init_db(db_path)
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO dictionary_entries (dictionary_type,line_number,raw_line) VALUES ('callsigns',1,'клен: 600')")
    setup,*_=dictionaries_from(db_path,archive)
    assert setup[0].number_rules=={},'двох джерел tune недостатньо; holdout не є третім голосом'


def test_lost_leading_zero_of_the_same_code_is_restored():
    cs,commands=dictionaries()
    assert tidy_text('грім 22, прийом',cs,commands)=='ГРІМ 0 22, прийом'
    # половину пари лишає command_completion — з позначкою «почуто»
    assert constrain_dictionary_text('злива 42, прийом',cs,commands)[0]=='злива 42, прийом'


def test_lost_group_needs_unique_code_and_same_digits():
    cs,_=dictionaries()
    commands=command_dictionary(['грім 0 22','грім 22'])
    assert constrain_dictionary_text('грім 22',cs,commands)[0]=='грім 22'      # «22» сам дозволений код
    commands=command_dictionary(['злива 42 43'])
    assert constrain_dictionary_text('злива 42',cs,commands)[0]=='злива 42'    # інша група не дописується
    commands=command_dictionary(['грім 0 22'])
    assert constrain_dictionary_text('грім 23',cs,commands)[0]=='грім 23'      # не найближче число


def test_zero_glued_to_command_name_is_split_only_for_an_exact_code():
    cs,_=dictionaries()
    commands=command_dictionary(['буря 0 11','буря 222','грім 22'])
    assert tidy_text('буряна 11, прийом',cs,commands)=='БУРЯ 0 11, прийом'
    assert tidy_text('буряна 11, буряна 11',cs,commands)=='БУРЯ 0 11, БУРЯ 0 11'
    assert constrain_dictionary_text('буряна резонанс',cs,commands)[0]=='буря 0 резонанс'   # решту дописує completion
    assert tidy_text('буряна 12, буряна 12, прийом',cs,commands)=='БУРЯ 0 11, БУРЯ 0 11, прийом'
    assert tidy_text('буряна 45',cs,commands)=='БУРЯ 0 11'   # єдиний код на нуль
    two=command_dictionary(['буря 0 11','буря 0 22'])
    assert constrain_dictionary_text('буряна 45',cs,two)[0]=='буряна 45'
    for raw in ('буряна 5','буряна 13 13','буряна 120','грімна 12','грімна 22','буряна 222','«буряна 11»','забуряна 11'):
        assert constrain_dictionary_text(raw,cs,commands)[0]==raw,raw   # код не вигадується й не підбирається
