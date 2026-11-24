"""Перевірка оформлення тексту в редакторах (еталон в архіві, правка в Mini App).

Нічого не забороняє: повертає зауваження, які оператор може прийняти або
пропустити. Три види:

- ``format`` — оформлення за правилами docs/archive.md: пробіли, коми, цифри
  групами, «з», апостроф. Безпечні, їх можна виправити всі одразу;
- ``spelling`` — «можливо, опечатка»: слово схоже на позивний чи оборот зі
  словника/шаблонів. Лише окремо, за рішенням оператора;
- ``info`` — без виправлення: номера немає у словнику, російська літера тощо.

Для кожного зауваження з виправленням повертається ``fixed_text`` — увесь
текст лише з цим виправленням, тож клієнту не треба рахувати позиції.
"""

import re
import sqlite3

import shared.callsigns as callsigns_module
from shared.callsigns import FOLD, NUMBER_WORDS, basic_normalize
from shared.template_text import _TOKEN, _CODE_PAIR, template_text
from shared.daily_codes import remove_confirmed_code_commas
from shared.transcript_format import tidy_text, has_several_transmissions, TRANSMISSION_SEPARATOR, TRANSMISSION_SPLIT

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


def _callsign_cased(new, old):
    """Назва зі словника завжди повертається в установленому великому написанні позивного."""
    if old.isupper() and len(old) > 1:
        return new.upper()
    return new[:1].upper() + new[1:].lower()


def _format_rules():
    """(шаблон, заміна або функція, повідомлення) — по черзі, кожне окремим зауваженням."""
    return [
        (re.compile(r"(?<=\S)[ \t]{2,}(?=\S)"), " ", "Зайві пробіли"),
        (re.compile(r"[ \t]+(?=[,.;:!?])"), "", "Пробіл перед розділовим знаком"),
        # Кома між двома цифрами — окреме правило нижче.
        (re.compile(r"(?<=\D),(?=[^\s,])|(?<=\d),(?=[^\s,\d])"), ", ", "Після коми потрібен пробіл"),
        (re.compile(r"(?<![\w:./-])(\d{1,2})[,\-–—](\d{2})(?![\w:./-])"), r"\1 \2", "Цифри — групами через пробіл: «31 05», а не «31,05»"),
        (_CODE_PAIR, r"\1 \2",
         "Чотири цифри — парами, як вимовляють: «42 42», а не «4242»"),
        (re.compile(r"(?<![\w:.,/-])0(\d\d)(?![\w:/-]|[.,]\d)"), r"0 \1", "Номер з нулем — як вимовляють: «0 22», а не «022»"),
        (re.compile(r"(?<![\w’'ʼ`])([сС])(?![\w’'ʼ`])"), lambda m: "з" if m[1] == "с" else "З",
         "Українською — «з», а не «с»"),
    ]


def _issue(kind, message, text=None, fixed=None):
    item = {"kind": kind, "message": message}
    if fixed is not None and fixed != text:
        item["fixed_text"] = fixed
    return item


def _format_issues(text, codes=None, dictionaries=()):
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
    code_fixed = remove_confirmed_code_commas(text, codes, dictionaries)
    if code_fixed != text:
        issues.append(_issue("format", "Кома всередині підтвердженого коду доби", text, code_fixed))
    return issues


def format_text(text, codes=None, dictionaries=()):
    """Усі безпечні виправлення оформлення разом (до стабільного стану)."""
    text = remove_confirmed_code_commas(text, codes, dictionaries)
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
            new = _callsign_cased(name, match.group())
            issues.append(_issue("spelling", f"Можливо, «{new}», а не «{match.group()}»?", text,
                                 text[:match.start()] + new + text[match.end():]))
    return issues


def _spelling_issues(text, callsigns, known):
    """Слова, які шаблони написали б інакше: «Бериза 56» → «Береза 56»."""
    if callsigns is None:
        return []
    # Цифри парами («4862» → «48 62») — справа оформлення, не описка: порівнюємо вже з парами.
    from shared.template_text import split_code_pairs
    text = split_code_pairs(text)
    issues = _dictionary_spelling(text, set(callsigns.names) | set(known))
    canonical_callsigns = {name.translate(FOLD): name for name in callsigns.names}
    suggested = template_text(text, callsigns, known)
    if suggested == text:
        return issues
    old = list(_TOKEN.finditer(text))
    new = [m.group() for m in _TOKEN.finditer(suggested)]
    if len(old) != len(new):
        return issues + [_issue("spelling", "Можливо, опечатка: перевірте позивні й звороти", text, suggested)]
    for match, word in zip(old, new):
        canonical = canonical_callsigns.get(word.translate(FOLD))
        if canonical:
            word = _callsign_cased(canonical, match.group())
        if match.group() != word:
            issues.append(_issue("spelling", f"Можливо, «{word}», а не «{match.group()}»?", text,
                                 text[:match.start()] + word + text[match.end():]))
    return issues


def _number_issues(text, dictionaries, callsign_dictionary=None):
    """Позивний чи команда зі словника з номером, якого в словнику немає."""
    from shared.command_completion import repeated_code_completions
    completed_starts = {start for dictionary in dictionaries if dictionary.anchored_threshold is not None
                        for start, _, _ in repeated_code_completions(text, callsign_dictionary, dictionary)}
    matches = list(_TOKEN.finditer(text))
    tokens = [m.group() for m in matches]
    issues, seen = [], set()
    for i, word in enumerate(tokens[:-1]):
        if word.isdigit() or not tokens[i + 1].isdigit():
            continue
        if matches[i+1].start() in completed_starts:
            continue
        heard = basic_normalize(word)
        if heard in callsigns_module.ORDINALS:
            continue    # «четвертий» = дуб 4; число після нього — не номер позивного
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
        issue = _issue("info", f"«{name} {tokens[i + 1]}»: номера {tokens[i + 1]} немає у словнику «{name}». "
                              "Перевірте на слух; якщо номер правильний — додайте його в словник.")
        # Кнопка лише для позивного: не перетворюємо код команди на новий позивний.
        if (callsign_dictionary is not None and name in callsign_dictionary.names
                and not any(name in d.names for d in dictionaries if d is not callsign_dictionary)):
            issue["dictionary_entry"] = f"{name}: {tokens[i + 1]}"
        issues.append(issue)
    return issues



_CODE_TOKEN = re.compile(r"[^\W\d_]+(?:[’'ʼ`][^\W\d_]+)*|\d+")


def _message_pairs(text, names):
    """Пари цифр повідомлення («48 62», «48, 62», «4862») з позиціями; номер одразу
    після назви словника («Береза 68, 31 05» -> 68) — номер позивного, не код."""
    tokens = [(m.group(), m.start(), m.end()) for m in _CODE_TOKEN.finditer(text)]
    pairs, i = [], 0
    while i < len(tokens):
        token, start, end = tokens[i]
        if not token.isdigit():
            i += 1
            folded = basic_normalize(token).translate(FOLD)
            alias = callsigns_module.CALLSIGN_ALIASES.get(basic_normalize(token))
            ordinal = basic_normalize(token) in callsigns_module.ORDINALS
            if (folded in names or (alias and not ordinal)) and i < len(tokens) and tokens[i][0].isdigit():
                i += 1
            continue
        if len(token) == 4:
            pairs.append((token, start, end)); i += 1; continue
        if len(token) == 2 and i + 1 < len(tokens) and tokens[i + 1][0].isdigit() and len(tokens[i + 1][0]) == 2:
            pairs.append((token + tokens[i + 1][0], start, tokens[i + 1][2])); i += 2; continue
        i += 1
    return pairs


def _code_issues(text, codes, names):
    """«Можливо, 48 62?» — пара цифр відрізняється від коду доби однією цифрою."""
    digits = [c.replace(" ", "") for c in codes or ()]
    issues = []
    for pair, start, end in _message_pairs(text, names):
        if pair in digits:
            continue
        near = [c for c in digits if sum(x != y for x, y in zip(c, pair)) == 1]
        if len(near) == 1:
            code = f"{near[0][:2]} {near[0][2:]}"
            issue = _issue("spelling", f"Можливо, «{code}» — код доби, а не «{text[start:end]}»?", text,
                           text[:start] + code + text[end:])
            issue["manual_only"] = True
            issues.append(issue)
    return issues


# Слова, які стоять перед числом, але не є позивним: службові, звороти, одиниці.
NOT_CALLSIGN_WORDS = {"я", "на", "до", "як", "та", "і", "й", "не", "з", "по", "о", "в", "у", "за", "від", "це",
                      "прийом", "прийнято", "прийняв", "номер", "код", "квадрат", "точка", "хвилин", "хвилини",
                      "секунд", "метрів", "кілометрів", "година", "години", "раз", "рази", "разів",
                      "позивний", "позивного", "позивні", "назвіть", "кодом"}


def _unknown_callsign_issues(text, callsigns, other_names, covered):
    """Слово перед номером, якого немає в жодному словнику: можливо, новий позивний.

    Лише підказка з готовим рядком для словника; оператор перевіряє на слух.
    ``covered`` — слова, для яких уже є підказка написання (схоже на відомий позивний).
    """
    if callsigns is None:
        return []
    known = {n.translate(FOLD) for n in (*callsigns.names, *other_names)}
    tokens = [m.group() for m in _TOKEN.finditer(text)]
    issues, seen = [], set()
    for i, word in enumerate(tokens[:-1]):
        if word.isdigit() or not tokens[i + 1].isdigit():
            continue
        heard = basic_normalize(word)
        folded = heard.translate(FOLD)
        if (len(heard) < 3 or not heard.isalpha() or folded in known or heard in seen or heard in covered
                or heard in NOT_CALLSIGN_WORDS or heard in NUMBER_WORDS or heard in callsigns_module.ORDINALS
                or callsigns_module.CALLSIGN_ALIASES.get(heard) or heard in callsigns_module.dictionary_address_forms(known)):
            continue
        seen.add(heard)
        # «Вишня, 42 42» — позивний і код (дві пари цифр), а не номер позивного.
        code = (len(tokens[i + 1]) == 2 and i + 2 < len(tokens) and tokens[i + 2].isdigit() and len(tokens[i + 2]) == 2)
        if code:
            issue = _issue("info", f"«{heard}» немає у словнику позивних. Якщо це позивний (далі — код), додайте його без номера.")
            issue["dictionary_entry"] = heard
        else:
            issue = _issue("info", f"«{heard}» немає у словнику позивних. Якщо це позивний — додайте його з номером {tokens[i + 1]}.")
            issue["dictionary_entry"] = f"{heard}: {tokens[i + 1]}"
        issues.append(issue)
    return issues


def check_text(text, callsigns=None, commands=None, watch=None, codes=None):
    """Зауваження до тексту оператора; ``formatted`` — текст з усіма виправленнями оформлення."""
    text = text or ""
    dictionaries = tuple(d for d in (callsigns, commands, watch) if d is not None)
    known = tuple((commands.names if commands else ())) + tuple((watch.names if watch else ()))
    issues = _format_issues(text, codes, dictionaries)
    from shared.dictionary_blocks import constrain_dictionary_text
    constrained,block_changes=constrain_dictionary_text(text,callsigns,commands)
    for change in block_changes:
        # Поділ того самого числа на групи вже має окрему підказку оформлення.
        if change['kind']=='command_block' and ''.join(re.findall(r'\d',change['heard']))==''.join(re.findall(r'\d',change['expected'])):
            continue
        message=(f"Номер позивного за {change['sources']} незалежними еталонами" if change['kind']=='callsign_number'
                 else 'Повний блок команди зі словника' if change['kind']=='command_block' else 'Число словами — цифрами')
        issue=_issue('info',f"{message}: «{change['heard']}» → «{change['expected']}»",text,constrained)
        issue['manual_only']=True
        issues.append(issue)
    from shared.command_completion import repeated_code_completions
    for start, end, full in repeated_code_completions(text, callsigns, commands):
        issues.append(_issue('format', f'Відновлення повтору коду команди за словником: «{text[start:end]}» → «{full}»',
                             text, text[:start] + full + text[end:]))
    issues += _spelling_issues(text, callsigns, known)
    issues += _number_issues(constrained, [d for d in (callsigns, commands) if d is not None], callsigns)
    covered = {basic_normalize(m) for i in issues if i["kind"] == "spelling"
               for m in re.findall(r"а не «([^»]+)»", i["message"])}
    issues += _unknown_callsign_issues(text, callsigns, known, covered)
    names = {n.translate(FOLD) for d in (callsigns, commands, watch) if d is not None for n in d.names}
    issues += _code_issues(text, codes, names)
    for letter in sorted(set(RUSSIAN_LETTERS.findall(text))):
        issues.append(_issue("info", f"Російська літера «{letter}» — перевірте, чи це українське слово"))
    tidy = tidy_text(text, callsigns, commands,constrain_blocks=False)
    formatted_text = format_text(text, codes, dictionaries)
    tidy_after_format = tidy_text(formatted_text, callsigns, commands)
    case_only_duplicate = (
        any(issue["kind"] == "spelling" for issue in issues)
        and tidy_after_format.casefold() == formatted_text.casefold()
    )
    if tidy != text and tidy_after_format != formatted_text and not case_only_duplicate:
        issues.append(_issue("format", "Коми між групами й великі літери позивних", text, tidy))
    formatted = tidy_after_format
    return {"issues": issues, "formatted": formatted if formatted != text else None}


_cache = {}


def highlight_spans(text, callsigns, commands, issues=()):
    """Підсвічування точних слів та місць підказок; межі UTF-16 для браузера.

    Не використовується для розпізнавання/навчання. Немає часових припущень.
    """
    spans = []
    def add(start, end, kind):
        if end > start:
            offset = lambda n: len(text[:n].encode('utf-16-le')) // 2
            spans.append({'start': offset(start), 'end': offset(end), 'kind': kind})
    names = {basic_normalize(n).translate(FOLD) for n in callsigns.names} if callsigns else set()
    if callsigns:
        names.update(basic_normalize(n).translate(FOLD) for n in callsigns_module.dictionary_address_forms(callsigns.names))
    command_names = {basic_normalize(n).translate(FOLD) for n in commands.names} if commands else set()
    tokens = list(_TOKEN.finditer(text))
    for i, token in enumerate(tokens):
        word = basic_normalize(token.group()).translate(FOLD)
        if word in command_names and i + 1 < len(tokens) and tokens[i+1].group().isdigit():
            add(token.start(), token.end(), 'command')
        elif word in names:
            add(token.start(), token.end(), 'callsign')
    for match in re.finditer(r'(?<!\S)[/|](?!\S)', text):
        add(match.start(), match.end(), 'boundary')
    for issue in issues:
        if issue['kind'] == 'format':
            continue
        fixed = issue.get('fixed_text')
        if fixed is not None:
            # Підказка зазвичай змінює одне слово. Лінійні межі відмінності
            # замість квадратичного diff на довгих повторюваних розшифровках.
            a = 0
            while a < min(len(text), len(fixed)) and text[a] == fixed[a]:
                a += 1
            b, tail = len(text), len(fixed)
            while b > a and tail > a and text[b-1] == fixed[tail-1]:
                b -= 1
                tail -= 1
            for token in tokens:
                if token.start() < b and token.end() > a:
                    add(token.start(), token.end(), 'problem')
        else:
            quoted = re.search('«([^»]+)»', issue['message'])
            if quoted:
                for match in re.finditer(re.escape(quoted[1]), text, re.I):
                    add(match.start(), match.end(), 'problem')
    return sorted((dict(zip(('start','end','kind'), item)) for item in
                   {(s['start'], s['end'], s['kind']) for s in spans}), key=lambda s: (s['start'],s['end'],s['kind']))


def dictionaries_from_db(db, archive=None):
    """(позивні, команди, Telegram) з тієї самої SQLite; кешується за відбитком рядків."""
    from shared.dictionary_loader import dictionary_fingerprint, dictionaries_from_lines, read_dictionary_lines
    import shared.callsigns as callsign_module
    from shared.text_lists import sync_aliases
    sync_aliases(db)
    from pathlib import Path
    location=db.execute('PRAGMA database_list').fetchone()[2]
    if archive is None and location:archive=Path(location).parent/'.local-recordings'
    try:
        key = (location,str(archive),dictionary_fingerprint(db), id(callsign_module.ALIASES))
    except sqlite3.OperationalError:
        return None, None, None
    dictionaries = _cache.get(key)
    if dictionaries is None:
        dictionaries = dictionaries_from_lines(read_dictionary_lines(db))
        _cache.clear()
        _cache[key] = dictionaries
    from shared.dictionary_number_evidence import load_rules
    dictionaries[0].number_rules=load_rules(archive,dictionaries[0],dictionaries[1]) if archive else {}
    return dictionaries



def check_with_db(db, text, codes=None, archive=None):
    return check_text(text, *dictionaries_from_db(db,archive), codes=codes)
