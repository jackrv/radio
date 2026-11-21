"""Приватні знімки вибірок за згадками; без зміни текстів і розмітки."""
import json
import os
from pathlib import Path
import re
import time
import uuid
import fcntl
from contextlib import closing

from core.text_metrics import metric_words
from services.sqlite_connection import read_only
from services.private_files import private_open

# Вихід ASR на позначеному шумі зберігається для оцінки хибного тексту,
# але це не почуте мовлення й не має потрапляти до робочої вибірки.
SPEECH_FILTER = "(label IS NULL OR label NOT IN ('noise','notification','room'))"


def selection_order(order):
    if order not in ('old', 'new', 'unreviewed'):
        raise ValueError('Некоректний порядок вибірки')
    direction = 'DESC' if order == 'new' else 'ASC'
    return ('reference IS NOT NULL ASC, ' if order == 'unreviewed' else '') + f'created {direction},id {direction}'


def effective_text(reference, data):
    if reference is not None:
        return reference
    final = data.get('final_text')
    return final if isinstance(final, str) else (data.get('raw_text') or '')


def matching_terms(text, terms):
    normalized = ' ' + ' '.join(metric_words(text)) + ' '
    return [term for term in terms if ' ' + ' '.join(metric_words(term)) + ' ' in normalized]


def register_matcher(db, terms):
    needles = [' ' + ' '.join(metric_words(term)) + ' ' for term in terms]
    def matches(reference, raw):
        text = ' ' + ' '.join(metric_words(effective_text(reference, json.loads(raw)))) + ' '
        return int(any(needle in text for needle in needles))
    db.create_function('mention_matches', 2, matches, deterministic=True)


def find(root, terms, since, until, *, order='old', offset=0, limit=100):
    terms = list(dict.fromkeys(t.strip() for t in re.split(r'[,;\n]', terms) if t.strip()))
    if not terms or len(terms)>30 or any(len(t)>100 or not metric_words(t) for t in terms):
        raise ValueError('Укажіть 1–30 згадок, кожну до 100 символів, через кому або новий рядок')
    if not (0 <= since < until <= 4e9) or not 0 <= offset <= 1_000_000 or not 1 <= limit <= 100:
        raise ValueError('Перевірте період, порядок і сторінку вибірки')
    needles=[(term,' '+' '.join(metric_words(term))+' ') for term in terms]
    matched, ids = [], []
    sorting = selection_order(order)
    with closing(read_only(Path(root)/'index.sqlite3')) as db:
        for record_id, created, reference, label, data in db.execute(
                f'SELECT id,created,reference,label,data FROM recordings WHERE created>=? AND created<? AND {SPEECH_FILTER} ORDER BY {sorting}', (since, until)):
            data = json.loads(data)
            text = effective_text(reference, data)
            normalized=' '+' '.join(metric_words(text))+' '
            hits=[term for term,needle in needles if needle in normalized]
            if not hits: continue
            ids.append(record_id)
            if offset <= len(ids)-1 < offset+limit:
                matched.append({'id': record_id, 'created': created, 'text': text,
                                'label': label, 'reviewed': reference is not None, 'mentions': hits})
    return {'items': matched, 'total': len(ids), 'ids': ids, 'terms': terms, 'since': since, 'until': until, 'order': order}


def save_selection(db_path, result):
    root = Path(db_path).resolve().parent/'.local-selections'
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink(): raise ValueError('Каталог вибірок не може бути посиланням')
    root.chmod(0o700)
    token = uuid.uuid4().hex
    value = {key: result[key] for key in ('ids','terms','since','until','order')}
    value['created'] = time.time()
    payload=json.dumps(value,ensure_ascii=False)
    size=len(payload.encode('utf-8'));budget=50*1024**2
    if size>budget: raise ValueError('Вибірка завелика; звузьте період')
    with os.fdopen(private_open(root/'state.lock'), 'a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        files=[]
        for path in root.glob('*.json'):
            if not re.fullmatch('[0-9a-f]{32}\\.json',path.name) or path.is_symlink():continue
            info=path.stat()
            if time.time()-info.st_mtime>86400: path.unlink(missing_ok=True)
            else:files.append((info.st_mtime,info.st_size,path))
        files.sort();used=sum(item[1] for item in files)
        while files and (len(files)>=200 or used+size>budget):
            _,old_size,path=files.pop(0);path.unlink(missing_ok=True);used-=old_size
        with os.fdopen(private_open(root/(token+'.json'), os.O_CREAT|os.O_EXCL|os.O_WRONLY), 'w') as out:
            out.write(payload)
    return token


def load_selection(db_path, token):
    if not re.fullmatch('[0-9a-f]{32}', token): raise ValueError('Некоректний ID вибірки')
    path = Path(db_path).resolve().parent/'.local-selections'/(token+'.json')
    try:
        with os.fdopen(os.open(path, os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))) as src: value=json.load(src)
    except FileNotFoundError: raise ValueError('Вибірку не знайдено; повторіть пошук') from None
    if time.time()-value['created'] > 86400: raise ValueError('Вибірка застаріла; повторіть пошук')
    return value


def page_selection(root, saved, offset, limit, *, order=None):
    if not 0<=offset<=1_000_000 or not 1<=limit<=100: raise ValueError('Некоректна сторінка')
    order = saved['order'] if order is None else order
    sorting = selection_order(order)
    with closing(read_only(Path(root)/'index.sqlite3')) as db:
        db.execute('BEGIN')
        register_matcher(db, saved['terms'])
        args=(json.dumps(saved['ids']),)
        where='id IN (SELECT value FROM json_each(?)) AND '+SPEECH_FILTER+' AND mention_matches(reference,data)'
        total=db.execute('SELECT COUNT(*) FROM recordings WHERE '+where,args).fetchone()[0]
        rows=db.execute(f'SELECT id,created,reference,label,data FROM recordings WHERE {where} ORDER BY {sorting} LIMIT ? OFFSET ?',
                        (*args,limit,offset)).fetchall()
    items=[]
    for record_id,created,reference,label,raw in rows:
        text = effective_text(reference, json.loads(raw))
        hits = matching_terms(text, saved['terms'])
        items.append({'id':record_id,'created':created,'text':text,'label':label,'reviewed':reference is not None,'mentions':hits})
    return {'items':items,'total':total,'order':order}
