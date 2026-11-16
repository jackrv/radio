"""Перевірка оформлення тексту в редакторах (еталон в архіві, правка в Mini App).

Нічого не забороняє: повертає зауваження, які оператор може прийняти або
пропустити. Три види:

- ``format`` — оформлення за правилами RECORDINGS.md: пробіли, коми, цифри
  групами, «з», апостроф. Безпечні, їх можна виправити всі одразу;
- ``spelling`` — «можливо, опечатка»: слово схоже на позивний чи оборот зі
  словника/шаблонів. Лише окремо, за рішенням оператора;
- ``info`` — без виправлення: номера немає у словнику, російська літера тощо.

Для кожного зауваження з виправленням повертається ``fixed_text`` — увесь
текст лише з цим виправленням, тож клієнту не треба рахувати позиції.
"""

import re
import sqlite3

import callsigns as callsigns_module
from callsigns import FOLD, Callsigns, basic_normalize, command_dictionary
from core.template_text import _TOKEN, template_text

# Слова з апострофом, які часто пишуть без нього (ключ — без апострофа).
APOSTROPHE_WORDS = {w.replace("ʼ", ""): w for w in (
    "звʼязок", "звʼязку", "звʼязком", "звʼязки", "звʼязків", "пʼять", "пʼятий", "пʼята",
    "пʼятірка", "пʼятірку", "пʼятнадцять", "пʼятдесят", "пʼятсот", "девʼять", "девʼятий",
    "девʼятнадцять", "девʼяносто", "девʼятсот", "обʼєкт", "обʼєкта", "памʼять", "імʼя",
    "підʼїзд", "мʼясо", "бʼє", "вʼїзд", "сімʼя",
)}
RUSSIAN_LETTERS = re.compile(r"[ыэъёЫЭЪЁ]")


def _cased(new, old):
    if old.isupper() and len(old) > 1:
        return new.upper()
    return new[:1].upper() + new[1:] if old[:1].isupper() else new


def _format_rules():
    """(шаблон, заміна або функція, повідомлення) — по черзі, кожне окремим зауваженням."""
    return [
        (re.compile(r"(?<=\S)[ \t]{2,}(?=\S)"), " ", "Зайві пробіли"),
        (re.compile(r"[ \t]+(?=[,.;:!?])"), "", "Пробіл перед розділовим знаком"),
        # Кома між двома цифрами — окреме правило нижче.
        (re.compile(r"(?<=\D),(?=[^\s,])|(?<=\d),(?=[^\s,\d])"), ", ", "Після коми потрібен пробіл"),
        (re.compile(r"(?<=\d)[,\-–—](?=\d)"), " ", "Цифри — групами через пробіл: «96 80», а не «96,80»"),
        (re.compile(r"(?<!\d)(\d\d)(\d\d)(?!\d)"), r"\1 \2",
         "Чотири цифри — парами, як вимовляють: «42 42», а не «4242»"),
        (re.compile(r"(?<!\d)0(\d\d)(?!\d)"), r"0 \1", "Номер з нулем — як вимовляють: «0 22», а не «022»"),
        (re.compile(r"(?<![\w’'ʼ`])([сС])(?![\w’'ʼ`])"), lambda m: "з" if m[1] == "с" else "З",
         "Українською — «з», а не «с»"),
    ]


def _issue(kind, message, text=None, fixed=None):
    item = {"kind": kind, "message": message}
    if fixed is not None and fixed != text:
        item["fixed_text"] = fixed
    return item


def _format_issues(text):
    issues = []
    for pattern, replacement, message in _format_rules():
        for match in pattern.finditer(text):
            new = match.expand(replacement) if isinstance(replacement, str) else replacement(match)
            fixed = text[:match.start()] + new + text[match.end():]
            shown = f"{message}: «{match.group()}» → «{new}»" if re.search(r"\w", match.group()) else message
            issues.append(_issue("format", shown, text, fixed))
    for match in re.finditer(r"[^\W\d_]+", text):
        word = match.group()
        proper = APOSTROPHE_WORDS.get(word.lower())
        if proper:
            new = _cased(proper, word)
            issues.append(_issue("format", f"Пропущено апостроф: «{word}» → «{new}»", text,
                                 text[:match.start()] + new + text[match.end():]))
    return issues


def format_text(text):
    """Усі безпечні виправлення оформлення разом (до стабільного стану)."""
    for _ in range(5):
        before = text
        for pattern, replacement, _message in _format_rules():
            text = pattern.sub(replacement, text)
        text = re.sub(r"[^\W\d_]+", lambda m: _cased(APOSTROPHE_WORDS[m.group().lower()], m.group())
                      if m.group().lower() in APOSTROPHE_WORDS else m.group(), text)
        if text == before:
            break
    return text


def _dictionary_spelling(text, names):
    """Назва зі словника, написана з іншими «і/и», «е/є» тощо: «Клєн» → «Клен»."""
    folded = {name.translate(FOLD): name for name in names}
    issues = []
    for match in re.finditer(r"[^\W\d_]+(?:[’'ʼ`][^\W\d_]+)*", text):
        heard = basic_normalize(match.group())
        name = folded.get(heard.translate(FOLD))
        if name and name != heard and heard not in names:
            new = _cased(name, match.group())
            issues.append(_issue("spelling", f"Можливо, «{new}», а не «{match.group()}»?", text,
                                 text[:match.start()] + new + text[match.end():]))
    return issues


def _spelling_issues(text, callsigns, known):
    """Слова, які шаблони написали б інакше: «Бериза 56» → «Береза 56»."""
    if callsigns is None:
        return []
    issues = _dictionary_spelling(text, set(callsigns.names) | set(known))
    suggested = template_text(text, callsigns, known)
    if suggested == text:
        return issues
    old = list(_TOKEN.finditer(text))
    new = [m.group() for m in _TOKEN.finditer(suggested)]
    if len(old) != len(new):
        return issues + [_issue("spelling", "Можливо, опечатка: перевірте позивні й звороти", text, suggested)]
    for match, word in zip(old, new):
        if match.group() != word:
            issues.append(_issue("spelling", f"Можливо, «{word}», а не «{match.group()}»?", text,
                                 text[:match.start()] + word + text[match.end():]))
    return issues


def _number_issues(text, dictionaries):
    """Позивний чи команда зі словника з номером, якого в словнику немає."""
    tokens = [m.group() for m in _TOKEN.finditer(text)]
    issues, seen = [], set()
    for i, word in enumerate(tokens[:-1]):
        if word.isdigit() or not tokens[i + 1].isdigit():
            continue
        heard = basic_normalize(word)
        # Алиас — лише для слова, яке саме не є назвою словника (назва команди).
        own = any(heard in dictionary.names for dictionary in dictionaries)
        name = heard if own else callsigns_module.CALLSIGN_ALIASES.get(heard, heard)
        allowed = set()
        for dictionary in dictionaries:
            allowed |= set(dictionary.numbers.get(name) or ())
        if not allowed:
            continue
        candidates = [tokens[i + 1]]
        if i + 2 < len(tokens) and tokens[i + 2].isdigit():
            candidates.append(tokens[i + 1] + tokens[i + 2])
        if any(c in allowed for c in candidates) or (name, candidates[0]) in seen:
            continue
        seen.add((name, candidates[0]))
        issues.append(_issue("info", f"«{name} {tokens[i + 1]}»: номера {tokens[i + 1]} немає у словнику «{name}». "
                                     "Перевірте на слух; якщо номер правильний — додайте його в словник."))
    return issues


def check_text(text, callsigns=None, commands=None, watch=None):
    """Зауваження до тексту оператора; ``formatted`` — текст з усіма виправленнями оформлення."""
    text = text or ""
    known = tuple((commands.names if commands else ())) + tuple((watch.names if watch else ()))
    issues = _format_issues(text)
    issues += _spelling_issues(text, callsigns, known)
    issues += _number_issues(text, [d for d in (callsigns, commands) if d is not None])
    for letter in sorted(set(RUSSIAN_LETTERS.findall(text))):
        issues.append(_issue("info", f"Російська літера «{letter}» — перевірте, чи це українське слово"))
    formatted = format_text(text)
    return {"issues": issues, "formatted": formatted if formatted != text else None}


_cache = {}


def dictionaries_from_db(db):
    """(позивні, команди, Telegram) з тієї самої SQLite; кешується за відбитком рядків."""
    from services.dictionary_loader import dictionary_fingerprint
    from services.text_lists import sync_aliases
    sync_aliases(db)
    try:
        key = dictionary_fingerprint(db)
    except sqlite3.OperationalError:
        return None, None, None
    if key not in _cache:
        lines = {"callsigns": [], "commands": [], "telegram": []}
        for kind, line in db.execute("SELECT dictionary_type, raw_line FROM dictionary_entries "
                                     "WHERE enabled=1 ORDER BY line_number"):
            lines.setdefault(kind, []).append(line)
        _cache.clear()
        _cache[key] = (Callsigns.from_lines(lines["callsigns"]), command_dictionary(lines["commands"]),
                       Callsigns.from_lines(lines["telegram"]))
    return _cache[key]


def check_with_db(db, text):
    return check_text(text, *dictionaries_from_db(db))
