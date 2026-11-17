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
from callsigns import FOLD, basic_normalize
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


# ---------- Коми між групами й великі літери позивних ----------

_TIDY_TOKEN = re.compile(r"[^\W\d_]+(?:[’'ʼ`-][^\W\d_]+)*|\d+|[^\w\s]")
# Слова зворотів пишуться з малої (крім початку передачі): «…, Прийом» -> «…, прийом».
_LOWER_WORDS = {"прийом", "прийнято", "прийняв", "посилку", "отримав", "я", "на", "до", "як",
                "та", "і", "чую", "розбираю", "вас", "добре", "роботу", "роботи", "закінчив",
                "розпочав", "приступив", "повторіть", "голос", "не", "пройшов", "звʼязок", "звʼязку"}


def _tidy_part(part, callsigns, commands):
    """Одна передача: групи «позивний номер» / «команда номер» / цифри / текст — через кому."""
    tokens, comma, end = [], False, ""
    for raw in _TIDY_TOKEN.findall(part):
        if re.match(r"\w", raw):
            tokens.append([raw, comma]); comma = False
        elif raw in ".!?" :
            comma, end = True, raw
        else:
            comma = True
    if not tokens:
        return part.strip()
    if end and tokens and comma is False:
        end = ""                                   # крапка не в кінці — це просто розділювач
    fold = lambda word: basic_normalize(word).translate(FOLD)
    cs_names = {name.translate(FOLD): name for name in callsigns.names} if callsigns else {}
    cmd_names = {name.translate(FOLD): name for name in commands.names} if commands else {}

    def kind(i):
        word = tokens[i][0]
        if word.isdigit():
            return "num"
        f = fold(word)
        if f == "я":
            return "self"
        if f in cs_names:
            return "cs"
        if f in cmd_names and i + 1 < len(tokens) and tokens[i + 1][0].isdigit():
            return "cmd"                           # команда без номера — звичайне слово
        return "word"

    kinds = [kind(i) for i in range(len(tokens))]
    units, i = [], 0
    while i < len(tokens):
        k = kinds[i]
        start = i
        if k == "self" and i + 1 < len(tokens) and kinds[i + 1] == "cs":
            i, k = i + 1, "cs"
        if k == "cs":
            name = cs_names[fold(tokens[i][0])]
            i += 1
            has_numbers = bool(callsigns.numbers.get(name)) or name in callsigns.any_number
            if has_numbers and i < len(tokens) and kinds[i] == "num":
                i += 1
            units.append(("group", tokens[start:i]))
        elif k == "cmd":
            i += 1
            pair = (i + 1 < len(tokens) and kinds[i + 1] == "num" and len(tokens[i][0]) <= 2
                    and len(tokens[i + 1][0]) == 2)
            i += 2 if pair else 1                  # «0 22», «42 42» — код парами
            units.append(("group", tokens[start:i]))
        elif k == "num":
            while i < len(tokens) and kinds[i] == "num":
                i += 1
            units.append(("digits", tokens[start:i]))
        else:
            i += 1
            while i < len(tokens) and kinds[i] in ("word",) and not (
                    kinds[i] == "self" and i + 1 < len(tokens) and kinds[i + 1] == "cs"):
                i += 1
            run = tokens[start:i]
            if len(run) > 1 and fold(run[-1][0]) == "прийом":
                units += [("text", run[:-1]), ("text", run[-1:])]
            else:
                units.append(("text", run))

    out = []
    for unit_kind, run in units:
        words = []
        for j, (word, had_comma) in enumerate(run):
            f = fold(word)
            if f in cs_names:
                word = word[:1].upper() + word[1:].lower()
            elif basic_normalize(word) in _LOWER_WORDS or f in cmd_names:
                word = word.lower()
            separator = ", " if (unit_kind != "group" and j and had_comma) else " "
            words.append((separator if j else "") + word)
        out.append((unit_kind, run, "".join(words)))
    # Між звичайним словом і цифрами кому не додаємо: слово може бути позивним з
    # опечаткою («Бериза 56») — лишаємо, як було написано.
    text = ""
    for n, (unit_kind, run, rendered) in enumerate(out):
        if n:
            keep = out[n - 1][0] == "text" and unit_kind == "digits" and not run[0][1]
            text += " " if keep else ", "
        text += rendered
    return text[:1].upper() + text[1:] + end


# Роздільник передач у еталоні: « / ». Старий «|» лишається зрозумілим.
TRANSMISSION_SEPARATOR = "/"
TRANSMISSION_SPLIT = re.compile(r"\s*[|/]\s*")


def has_several_transmissions(text):
    return bool(TRANSMISSION_SPLIT.search(text or ""))


def tidy_text(text, callsigns, commands):
    """Коми між групами й великі літери позивних; слова й цифри не змінюються.

    Передачі, розділені « / » (старий роздільник «|» теж розуміється й замінюється
    на « / »), оформлюються окремо. Якщо порядок слів раптом
    змінився б, текст повертається без змін.
    """
    if not text or not text.strip() or callsigns is None:
        return text
    parts = TRANSMISSION_SPLIT.split(text)
    tidy = f" {TRANSMISSION_SEPARATOR} ".join(_tidy_part(p, callsigns, commands) for p in parts if p.strip())
    same = lambda a, b: re.findall(r"[^\W_]+", re.sub(r"[’'ʼ`]", "", a.casefold())) == \
        re.findall(r"[^\W_]+", re.sub(r"[’'ʼ`]", "", b.casefold()))
    return tidy if same(text, tidy) else text


_CODE_TOKEN = re.compile(r"[^\W\d_]+(?:[’'ʼ`][^\W\d_]+)*|\d+")


def _message_pairs(text, names):
    """Пари цифр повідомлення («14 77», «14, 77», «1477») з позиціями; номер одразу
    після назви словника («Береза 68, 96 80» -> 68) — номер позивного, не код."""
    tokens = [(m.group(), m.start(), m.end()) for m in _CODE_TOKEN.finditer(text)]
    pairs, i = [], 0
    while i < len(tokens):
        token, start, end = tokens[i]
        if not token.isdigit():
            i += 1
            folded = basic_normalize(token).translate(FOLD)
            if (folded in names or callsigns_module.CALLSIGN_ALIASES.get(basic_normalize(token))) \
                    and i < len(tokens) and tokens[i][0].isdigit():
                i += 1
            continue
        if len(token) == 4:
            pairs.append((token, start, end)); i += 1; continue
        if len(token) == 2 and i + 1 < len(tokens) and tokens[i + 1][0].isdigit() and len(tokens[i + 1][0]) == 2:
            pairs.append((token + tokens[i + 1][0], start, tokens[i + 1][2])); i += 2; continue
        i += 1
    return pairs


def _code_issues(text, codes, names):
    """«Можливо, 14 77?» — пара цифр відрізняється від коду доби однією цифрою."""
    digits = [c.replace(" ", "") for c in codes or ()]
    issues = []
    for pair, start, end in _message_pairs(text, names):
        if pair in digits:
            continue
        near = [c for c in digits if sum(x != y for x, y in zip(c, pair)) == 1]
        if len(near) == 1:
            code = f"{near[0][:2]} {near[0][2:]}"
            issues.append(_issue("spelling", f"Можливо, «{code}» — код доби, а не «{text[start:end]}»?", text,
                                 text[:start] + code + text[end:]))
    return issues


def check_text(text, callsigns=None, commands=None, watch=None, codes=None):
    """Зауваження до тексту оператора; ``formatted`` — текст з усіма виправленнями оформлення."""
    text = text or ""
    known = tuple((commands.names if commands else ())) + tuple((watch.names if watch else ()))
    issues = _format_issues(text)
    issues += _spelling_issues(text, callsigns, known)
    issues += _number_issues(text, [d for d in (callsigns, commands) if d is not None])
    names = {n.translate(FOLD) for d in (callsigns, commands, watch) if d is not None for n in d.names}
    issues += _code_issues(text, codes, names)
    for letter in sorted(set(RUSSIAN_LETTERS.findall(text))):
        issues.append(_issue("info", f"Російська літера «{letter}» — перевірте, чи це українське слово"))
    tidy = tidy_text(text, callsigns, commands)
    if tidy != text and tidy_text(format_text(text), callsigns, commands) != format_text(text):
        issues.append(_issue("format", "Коми між групами й великі літери позивних", text, tidy))
    formatted = tidy_text(format_text(text), callsigns, commands)
    return {"issues": issues, "formatted": formatted if formatted != text else None}


_cache = {}


def dictionaries_from_db(db):
    """(позивні, команди, Telegram) з тієї самої SQLite; кешується за відбитком рядків."""
    from services.dictionary_loader import dictionary_fingerprint, dictionaries_from_lines, read_dictionary_lines
    import callsigns as callsign_module
    from services.text_lists import sync_aliases
    sync_aliases(db)
    try:
        key = (dictionary_fingerprint(db), id(callsign_module.ALIASES))
    except sqlite3.OperationalError:
        return None, None, None
    dictionaries = _cache.get(key)
    if dictionaries is None:
        dictionaries = dictionaries_from_lines(read_dictionary_lines(db))
        _cache.clear()
        _cache[key] = dictionaries
    return dictionaries



def check_with_db(db, text, codes=None):
    return check_text(text, *dictionaries_from_db(db), codes=codes)
