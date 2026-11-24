"""Текстовий повтор не повинен відбирати лише вдалі виходи Whisper."""
import json

from shared.callsigns import Callsigns
from shared.recordings import RecordingStore
from shared.text_replay import load_records, run


def test_empty_sources_remain_in_speech_and_negative_denominators(tmp_path):
    store = RecordingStore(tmp_path/'archive')
    with store.connect() as db:
        for number, label, reference in ((1,'speech','Клен 15, прийом'), (2,'noise',''), (3,'notification',''), (4,'room','')):
            db.execute('INSERT INTO recordings(id,created,status,bytes,data,reference,label) VALUES(?,?,?,?,?,?,?)',
                       (str(number)*32,1,'empty_text',0,json.dumps({'raw_text':'','final_text':''}),reference,label))
    speech = load_records(store.root)
    assert len(speech) == 1 and speech[0][2] == ''
    rows = load_records(store.root, include_non_speech=True)
    assert len(rows) == 4
    dictionaries = tuple(Callsigns.from_lines([]) for _ in range(3))
    metrics, _ = run(rows, dictionaries, set())
    assert metrics['records'] == 1 and metrics['missed'] == 1 and metrics['no_edits'] == 0
    assert metrics['non_speech_records'] == 3 and metrics['false_text'] == 0


def test_empty_draft_can_disable_filter_and_report_new_false_text():
    from shared.text_replay import compare
    dictionaries=tuple(Callsigns.from_lines([]) for _ in range(3))
    rows=[('noise','','синтетичний шелест','noise')]
    report=compare(rows,dictionaries,{'синтетичний шелест'},before={},after={},after_short_garbage=[])
    assert report['before']['false_text']==0 and report['after']['false_text']==1
    report=compare(rows,dictionaries,set(),before={},after={},blacklist=['синтетичний шелест'],after_blacklist=[])
    assert report['before']['false_text']==0 and report['after']['false_text']==1
