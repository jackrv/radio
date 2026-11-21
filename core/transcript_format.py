"""Спільне оформлення готового тексту для ефіру, лабораторії та редактора.

Не ділить WAV і не вгадує ролі. Команди обмежені точними словниковими блоками;
номер позивного змінюється лише за одностайними незалежними еталонами,
якщо початковий номер недопустимий, а цільовий дозволений чинним словником.
"""
import re
from core.callsigns import FOLD, basic_normalize, dictionary_address_forms
from core.command_completion import restore_repeated_command_codes
from core.dictionary_blocks import constrain_dictionary_text

# ---------- Коми між групами й великі літери позивних ----------

_TIDY_TOKEN = re.compile(r"[^\W\d_]+(?:[’'ʼ`-][^\W\d_]+)*|\d+|[^\w\s]")
# Слова зворотів пишуться з малої (крім початку передачі): «…, Прийом» -> «…, прийом».
_LOWER_WORDS = {"прийом", "прийнято", "прийняв", "посилку", "отримав", "я", "на", "до", "як",
                "та", "і", "чую", "розбираю", "вас", "добре", "роботу", "роботи", "закінчив",
                "розпочав", "приступив", "повторіть", "голос", "не", "пройшов", "звʼязок", "звʼязку"}

_LEXEME=re.compile(r"[^\W\d_]+(?:[’'ʼ`-][^\W\d_]+)*|\d+")
_PROTECTED=re.compile(r'\b\d+[:./-]\d+(?:[:./-]\d+)?\b|\bhttps?://|\b(?:19|20)\d\d\s+(?:рік|року|році|роках|р\.)',re.I)


def infer_transmission_boundaries(text,callsigns):
    """Лише завершений обмін із новим іменним заголовком та змістовним продовженням.

    «Прийом» усередині речення, повтор самого позивного, цитата або голий номер
    не є достатньою підставою. Це текстова гіпотеза, а не діаризація аудіо.
    """
    if callsigns is None or _PROTECTED.search(text):return text
    names={basic_normalize(n).translate(FOLD) for n in (*callsigns.names,*dictionary_address_forms(callsigns.names))}
    tokens=list(_LEXEME.finditer(text));words=[basic_normalize(m.group()).translate(FOLD) for m in tokens]
    boundaries=[];start=0
    for i,word in enumerate(words):
        if word!='прийом' or i+3>=len(tokens):continue
        between=text[tokens[i].end():tokens[i+1].start()]
        if not re.fullmatch(r'[\s,.;!?]+',between):continue
        prefix=text[start:tokens[i].end()]
        if prefix.count('«')>prefix.count('»') or prefix.count('"')%2:continue
        if not any(words[j] in names and words[j+1].isdigit() for j in range(len(words)-1)
                   if start<=tokens[j].start()<tokens[i].start()):continue
        p=i+1
        if words[p]=='я':p+=1
        if p+1>=len(words) or words[p] not in names or not words[p+1].isdigit():continue
        body=p+2
        # Другий позивний у заголовку — з «я» або без нього.
        q=body+1 if body<len(words) and words[body]=='я' else body
        if q+1<len(words) and words[q] in names and words[q+1].isdigit():body=q+2
        close=next((j for j in range(body,len(words)) if words[j]=='прийом'),None)
        if close is None:continue
        if not any(not words[j].isdigit() and words[j] not in names and words[j] not in {'я','прийом'}
                   for j in range(body,close)):continue
        boundaries.append((tokens[i].end(),tokens[i+1].start()))
        start=tokens[i+1].start()
    for a,b in reversed(boundaries):text=text[:a]+' / '+text[b:]
    return text


def _tidy_part(part, callsigns, commands):
    """Одна передача: групи «позивний номер» / «команда номер» / цифри / текст — через кому."""
    if _PROTECTED.search(part):return part.strip()
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
    # Сталий зворот зі списку «Обороти» («перевірка звʼязку», «чую та розбираю») пишеться без ком усередині.
    from core import template_text
    folded = [fold(word) for word, _ in tokens]
    for phrase in sorted((tuple(fold(w) for w in p.split()) for p in template_text.PHRASES if len(p.split()) > 1), key=len, reverse=True):
        for k in range(len(tokens) - len(phrase) + 1):
            if tuple(folded[k:k + len(phrase)]) == phrase:
                for j in range(k + 1, k + len(phrase)):
                    tokens[j][1] = False
    cs_names = {name.translate(FOLD): name for name in callsigns.names} if callsigns else {}
    cs_forms={basic_normalize(name).translate(FOLD) for name in dictionary_address_forms(callsigns.names)} if callsigns else set()
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
            if unit_kind == "group" and f in cmd_names:
                word = word.upper()
            elif f in cs_names or f in cs_forms:
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


def tidy_text(text, callsigns, commands, *, restore_codes=True, constrain_blocks=True):
    """Оформлення та погоджене відновлення словникових блоків.

    Передачі, розділені « / » (старий роздільник «|» теж розуміється й замінюється
    на « / »), оформлюються окремо. Якщо порядок слів раптом
    змінився б, текст повертається без змін.
    """
    if not text or not text.strip() or callsigns is None:
        return text
    if _PROTECTED.search(text):return text
    if constrain_blocks:text,_=constrain_dictionary_text(text,callsigns,commands)
    if restore_codes:
        text = restore_repeated_command_codes(text, callsigns, commands)
    text=infer_transmission_boundaries(text,callsigns)
    parts = TRANSMISSION_SPLIT.split(text)
    tidy = f" {TRANSMISSION_SEPARATOR} ".join(_tidy_part(p, callsigns, commands) for p in parts if p.strip())
    same = lambda a, b: re.findall(r"[^\W_]+", re.sub(r"[’'ʼ`]", "", a.casefold())) == \
        re.findall(r"[^\W_]+", re.sub(r"[’'ʼ`]", "", b.casefold()))
    return tidy if same(text, tidy) else text
