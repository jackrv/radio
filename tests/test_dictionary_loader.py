"""Спільний конструктор словників і незмінність read-only знімка."""
import hashlib
import sqlite3
from unittest.mock import Mock

import pytest

import shared.dictionary_loader as module
from lab.asr_lab import read_only


def test_load_dictionaries(tmp_path):
    path = tmp_path / 'events.db'
    module.init_db(path)
    data = {'callsigns': ['Вишня: 210', 'Клен'], 'commands': ['Дощ'], 'telegram': ['Клен']}
    with sqlite3.connect(path) as db:
        for kind, lines in data.items():
            db.executemany('INSERT INTO dictionary_entries (dictionary_type,line_number,raw_line) VALUES (?,?,?)',
                           [(kind, index, line) for index, line in enumerate(lines, 1)])
    log = Mock()
    callsigns, commands, watch, prompt = module.load_dictionaries(path, log)
    assert callsigns.names == ['вишня', 'клен']
    assert commands.names == ['дощ'] and watch.names == ['клен']
    assert prompt == ', '.join(sorted(set(callsigns.names) | set(commands.names) | set(watch.names)))
    log.info.assert_called_once()


def test_empty_dictionaries(tmp_path):
    log = Mock()
    with pytest.raises(RuntimeError, match='Словник позивних порожній'):
        module.load_dictionaries(tmp_path / 'events.db', log)
    log.error.assert_called_once()


def test_common_builder_reads_frozen_database_without_writing(tmp_path):
    path = tmp_path / 'snapshot.db'
    module.init_db(path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO dictionary_entries (dictionary_type,line_number,raw_line) VALUES ('commands',1,'дощ 4242')")
    before = hashlib.sha256(path.read_bytes()).digest()
    with read_only(path) as db:
        _, commands, _ = module.dictionaries_from_lines(module.read_dictionary_lines(db))
    assert commands.find('дощ 42 42')[0][:2] == ('дощ', '4242')
    assert hashlib.sha256(path.read_bytes()).digest() == before
