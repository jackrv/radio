"""Номери позивних: лише одностайні незалежні еталони й чинний словник.

Це контекст «конкретне ім'я + конкретний помилковий номер», не глобальна
заміна цифр. Архів читається, голоси/еталони не переписуються.
"""
from collections import defaultdict
from contextlib import closing
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time

import core.callsigns as names_module
from core.callsigns import to_items
from core.dictionary_blocks import callsign_name, callsign_lookup
from services.sqlite_connection import read_only

MIN_SOURCES=3
REFRESH_S=30
_cache={}
_lock=threading.RLock()


def build_rules(rows,callsigns,commands=None):
    votes=defaultdict(lambda:defaultdict(set))
    lookup=callsign_lookup(callsigns)
    def pairs(text):
        result=defaultdict(list);items=to_items(text)
        for i,(kind,word) in enumerate(items[:-1]):
            if kind!='word' or items[i+1][0]!='num':continue
            name,_=callsign_name(word,callsigns,lookup)
            if name and (commands is None or name not in commands.names):result[name].append(items[i+1][1])
        return result
    for row in rows:
        raw,reference=pairs(row['raw_text']),pairs(row['reference'])
        source=row.get('source') or 'archive:'+row['id']
        for name,heard in raw.items():
            expected=reference.get(name,())
            # Різна кількість згадок того самого імені — неоднозначне зіставлення.
            if len(heard)!=len(expected):continue
            for old,new in zip(heard,expected):votes[(name,old)][new].add(source)
    result={}
    for (name,old),targets in votes.items():
        if len(targets)!=1 or name in callsigns.any_number or old in callsigns.numbers.get(name,()):continue
        target,sources=next(iter(targets.items()))
        if len(sources)>=MIN_SOURCES and target in callsigns.numbers.get(name,()):
            result[(name,old)]={'target':target,'sources':len(sources)}
    return result


def load_rules(archive,callsigns,commands=None,*,allowed_ids=None):
    if callsigns is None or not isinstance(callsigns.names,(list,tuple,set,frozenset)):return {}
    index=Path(archive)/'index.sqlite3'
    if not index.is_file():return {}
    key=(str(index.resolve()),tuple((name,tuple(sorted(callsigns.numbers.get(name,())))) for name in callsigns.names),
         tuple(sorted(callsigns.any_number)),tuple(sorted(names_module.CALLSIGN_ALIASES.items())),
         tuple(sorted((key,tuple(value)) for key,value in names_module.ALIASES.items())),
         tuple(commands.names) if commands else (),
         frozenset(allowed_ids) if allowed_ids is not None else None)
    now=time.monotonic()
    with _lock:
        cached=_cache.get(key)
        if cached and now-cached[0]<REFRESH_S:return cached[1]
        try:
            with closing(read_only(index,timeout=1.0)) as db:
                rows=[]
                for record_id,reference,encoded in db.execute("SELECT id,reference,data FROM recordings WHERE label='speech' AND reference IS NOT NULL ORDER BY reviewed_at DESC LIMIT 10000"):
                    if allowed_ids is not None and record_id not in allowed_ids:continue
                    data=json.loads(encoded)
                    raw=data.get('raw_text')
                    if isinstance(raw,str):rows.append(dict(id=record_id,reference=reference,raw_text=raw,source=data.get('fix_id')))
            rules=build_rules(rows,callsigns,commands)
        except (sqlite3.Error,ValueError,TypeError,AttributeError) as exc:
            logging.getLogger('radio').warning('Еталони номерів недоступні; контекстні заміни вимкнено (%s)',type(exc).__name__)
            rules={}
        if len(_cache)>=8:_cache.clear()
        _cache[key]=(now,rules)
        return rules
