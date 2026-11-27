"""М'яка підстановка шаблонів у текст сповіщення.

Whisper часто чує позивні й обороти радіообміну з однією-двома хибними літерами:
«Бериза 56», «посилка отримав», «приєм». Тут такі слова пишуться так, як у
словнику позивних і в переліку оборотів (docs/dictionaries.md). Правила навмисно
обережні:

- номери не змінюються ніколи, позивний перед номером замінюється лише тоді,
  коли номер є в словнику цього позивного (або слово дуже схоже на назву);
- слово, яке саме є назвою зі словників чи словом обороту, не замінюється:
  «прийняв» не стає «прийнято», назва команди не стає позивним;
- оборот замінюється, лише якщо кожне його слово схоже на почуте;
- «прийом» пишеться замість схожого слова лише в кінці тексту;
- незрозумілі шматки лишаються як почуті.

Результат застосовується до пошуку позивних і команд та зберігається в події;
початковий вихід Whisper лишається в raw_text.
"""

import re

from rapidfuzz import fuzz

import shared.callsigns as callsigns_module
from shared import default_text_lists
from shared.callsigns import FOLD, NUMBER_WORDS, basic_normalize, dictionary_address_forms

# Обороти з підтверджених оператором шаблонів (docs/dictionaries.md). Робочий перелік —
# у SQLite (список «Шаблони зворотів» у панелі); початкові — shared/default_text_lists.txt.
DEFAULT_PHRASES = default_text_lists.enabled("phrase_templates")
PHRASES = DEFAULT_PHRASES


def configure_phrases(phrases):
    """Застосувати перелік зворотів зі SQLite (живе оновлення, без перезапуску)."""
    global PHRASES
    PHRASES = tuple(dict.fromkeys(" ".join(p.split()) for p in phrases if p and p.strip()))


# Зациклення Whisper: те саме слово 4+ рази поспіль. Службові звороти не чіпаються —
# кілька передач у записі справді повторюють «прийом».
_LOOP = re.compile(r"\b([^\W\d_]+)((?:[\s,.;!?]+\1\b){3,})", re.IGNORECASE)
_LOOP_KEEP = {"прийом", "прийнято", "прийняв", "так", "ні"}


def trim_loops(text):
    """«вишня, вишня, вишня, вишня» → «вишня, вишня»."""
    def keep_two(match):
        if match.group(1).lower() in _LOOP_KEEP:
            return match.group(0)
        second = re.match(r"[\s,.;!?]+" + re.escape(match.group(1)), match.group(2), re.IGNORECASE)
        return match.group(1) + second.group(0)
    return _LOOP.sub(keep_two, text)


# «на 109» на місці позивного: Whisper розірвав позивний на «на» й номер («Набат 109»).
_SPLIT = re.compile(r"(^|[,.;!?/]\s*|\bя\s+)(на)\s+(\d{3})\b", re.IGNORECASE)


def join_split_callsign(text, callsigns):
    """Лише якщо є позивний, що починається на «на», і цей номер є в його словнику.

    Номер лишається почутим; не на місці позивного (усередині фрази) нічого не змінюється.
    """
    names = [n for n in callsigns.names if n.startswith("на") and len(n) > 2]
    if not names:
        return text

    def fix(match):
        number = match.group(3)
        owners = [n for n in names if number in (callsigns.numbers.get(n) or ())]
        if len(owners) != 1:
            return match.group(0)
        return f"{match.group(1)}{_cased(owners[0], match.group(2))} {number}"
    return _SPLIT.sub(fix, text)
FUNCTION_WORDS = ("я", "на", "до", "як", "та", "і", "й", "не", "добре", "вас", "з",
                  # Оцінки якості зв’язку — різні слова, а не написання одного: «четвірку» ≠ «пʼятірку».
                  "одиницю", "двійку", "трійку", "четвірку", "пʼятірку", "відмінно", "погано", "задовільно",
                  # Інші форми того самого дієслова — теж почуте слово, не помилка: «як прийняли».
                  "прийняли", "прийняла", "приймаєш", "приймаєте")

# Пороги схожості (rapidfuzz.ratio, 0–100), підібрані на архіві 16.11.
CALLSIGN_WITH_ITS_NUMBER = 60    # «Берега 56» -> «Береза 56», якщо 56 є в словнику берези
CALLSIGN_OTHER_NUMBER = 80       # «бериза 99» -> «береза 99»: номер лишається почутим
PHRASE_WORD = 55                 # кожне слово обороту окремо
PHRASE_MULTI = 75                # оборот із кількох слів загалом
PHRASE_LAST = 70                 # одне слово в кінці тексту («приєм» -> «прийом»)
PHRASE_MIDDLE = 82               # одне слово всередині тексту

_TOKEN = re.compile(r"[^\W\d_]+(?:[’'ʼ`-][^\W\d_]+)*|\d+")


# Чотири цифри поспіль у радіообміні вимовляються парами: «4862» → «48 62» (як в
# еталонах оператора: 156 пар і жодного злитого числа). Час, дроби й частини слів не чіпаються.
_CODE_PAIR = re.compile(r"(?<![\w:.,/-])(\d{2})(\d{2})(?![\w:/-]|[.,]\d|\s*(?:р\.|рік|році|року|роках))")


# Крапку в кінці ставить Whisper; в еталонах оператора її немає — прибираємо одну (не «…»).
_FINAL_PERIOD = re.compile(r"(?<![.…])\.\s*$")


def strip_final_period(text):
    return _FINAL_PERIOD.sub("", text.rstrip())


def split_code_pairs(text):
    return _CODE_PAIR.sub(r"\1 \2", text)


# «прийом, прийом» майже завжди — почуте двічі «прийнято, прийом»: 18.11.2026 на 362 еталонах подвоєне
# «прийом» у тексті програми — 17 разів, в еталонах — лише у 2 фразах; заміна: 6 фраз краще, 1 гірше,
# «без правок» 30 → 31. Лише рівно два поспіль; розділювач (кома, пробіл) і регістр зберігаються.
_OVER_RUN = re.compile(r"(?<![^\W\d_])прийом(?:\s*,?\s*прийом)+(?![^\W\d_])", re.IGNORECASE)
_OVER_WORD = re.compile(r"прийом", re.IGNORECASE)


DOUBLE_OVER = True    # вимикач у панелі: TEXT_FIX_DOUBLE_OVER


def fix_double_over(text):
    if not DOUBLE_OVER:
        return text

    def run(match):
        words = _OVER_WORD.findall(match.group(0))
        if len(words) != 2:
            return match.group(0)
        return _OVER_WORD.sub(lambda m: "Прийнято" if m.group(0)[:1].isupper() else "прийнято", match.group(0), count=1)
    return _OVER_RUN.sub(run, text)


# Кінцевий оборот «прийнято, прийом», почутий Whisper як «прийом» + випадкове слово (22.11.2026,
# docs/reports/2026-11-22-closing-turn.md). Лише в самому кінці тексту; «випадкове» — слово поза
# словниками, зворотами й службовими словами. Правила вивчено на tune/train, обрано на tune/val,
# перевірено один раз на holdout: 0 гірших фраз, 0 змін на noise/notification/room.
#   R2  «… прийом прийом [прийом…]»     → «… прийнято, прийом»
#   R1  «… прийом <випадкове>»           → «… прийнято, прийом»
#   R3  «… <число|позивний> <випадкове> прийом» → «… <число|позивний> прийнято, прийом»
CLOSING_TURN = True   # вимикач у панелі: TEXT_FIX_CLOSING_TURN
_CLOSING_FAMILY = {"прийом", "прийнято", "прийняв", "я"}


def fix_closing_turn(text, is_known, is_callsign):
    """Кінцевий «прийнято, прийом» замість «прийом» + випадкового слова; решта тексту не змінюється."""
    if not CLOSING_TURN:
        return text
    tokens = list(_TOKEN.finditer(text))
    words = [basic_normalize(m.group()) for m in tokens]
    if len(words) < 2 or words[-2:] == ["прийнято", "прийом"]:
        return text

    def random_word(w):
        return not w.isdigit() and w not in _CLOSING_FAMILY and not is_known(w)

    start = None
    if words[-1] == "прийом" and words[-2] == "прийом":
        start = len(words) - 2
        while start > 0 and words[start - 1] == "прийом":
            start -= 1
    elif words[-2] == "прийом" and random_word(words[-1]):
        start = len(words) - 2
    elif (len(words) >= 3 and words[-1] == "прийом" and random_word(words[-2])
          and (words[-3].isdigit() or is_callsign(words[-3]))):
        start = len(words) - 2
    if start is None:
        return text
    first = tokens[start]
    accepted = "Прийнято" if first.group()[:1].isupper() else "прийнято"
    return text[:first.start()] + accepted + ", прийом" + text[tokens[-1].end():]


_NAME = r"([^\W\d_]{3,})"
_HUNDREDS_SPLIT = re.compile(_NAME + r",?\s+(\d00),\s*(\d{1,2})\b(?!\s*\d)")
_NAME_COMMA_NUMBER = re.compile(_NAME + r",\s+(\d{1,4})\b")


JOIN_NUMBERS = True   # вимикач у панелі: TEXT_FIX_JOIN_NUMBERS
# «Клен 700, і, Клен 708» — Whisper чує «я» перед своїм позивним як «і» (27.11.2026: в еталонах
# «і» лише 5 разів на 2289 мовлень, у тексті програми — 75). Окреме «і» прямо перед позивним
# зі словника → «я»: на архіві 11 фраз змінено, 10 краще, 0 гірше; немовлення — 1 з 1604.
I_BEFORE_CALLSIGN = True   # вимикач у панелі: TEXT_FIX_I_BEFORE_CALLSIGN
_I_WORD = re.compile(r"(^|[,.;:]\s*|\d\s+)([іІ]),?(\s+)(?=([^\W\d_]+))")


def fix_i_before_callsign(text, is_callsign):
    """«і» на початку, після розділового знака чи номера, прямо перед позивним → «я».

    «Клен 700 і Клен 708» → «Клен 700 я Клен 708»; «Дуб і Клен» (між словами) не чіпаємо.
    """
    if not I_BEFORE_CALLSIGN:
        return text

    def one(match):
        if not is_callsign(match.group(4)):
            return match.group(0)
        return match.group(1) + ("Я" if match.group(2) == "І" else "я") + match.group(3)

    return _I_WORD.sub(one, text)


def join_callsign_numbers(text, callsigns):
    """Номер позивного, розірваний Whisper, — разом, лише якщо такий номер є в словнику позивного.

    «Клен, 700, 73» → «Клен 773» (вимовлено «сімсот сімдесят три»), якщо 773 — номер клена;
    «Клен, 700» → «Клен 700», якщо 700 — номер клена. Цифри не змінюються, лише кома й розрив;
    далі за числом ще одне число («700, 72 59») — це код, не склеюємо. 18.11.2026 на 280
    еталонах: 1 фраза краще, 0 гірше, +1 знайдений позивний.
    """
    def allowed(word):
        name = basic_normalize(word)
        return callsigns.numbers.get(name) or () if name in callsigns.names else ()

    def hundreds(match):
        joined = str(int(match.group(2)) + int(match.group(3)))
        return f"{match.group(1)} {joined}" if joined in allowed(match.group(1)) else match.group(0)

    def comma(match):
        return f"{match.group(1)} {match.group(2)}" if match.group(2) in allowed(match.group(1)) else match.group(0)

    if not JOIN_NUMBERS:
        return text
    return _NAME_COMMA_NUMBER.sub(comma, _HUNDREDS_SPLIT.sub(hundreds, text))


def _fold(word):
    return basic_normalize(word).translate(FOLD)


def _cased(new, old):
    return new[:1].upper() + new[1:] if old[:1].isupper() else new


def _phrases():
    return [(tuple(p.split()), _fold(p)) for p in sorted(PHRASES, key=lambda p: -len(p.split()))]


def template_text(text, callsigns, known_names=()):
    """Текст, у якому схожі позивні й обороти написано як у словнику.

    ``callsigns`` — словник позивних (Callsigns): назви з номерами;
    ``known_names`` — назви з усіх словників, які не можна переписувати.
    """
    known_names = tuple(known_names)
    text = join_callsign_numbers(join_split_callsign(trim_loops(text), callsigns), callsigns)
    tokens = [(m.start(), m.end(), m.group()) for m in _TOKEN.finditer(text)]
    if not tokens:
        return text
    phrases = _phrases()
    vocabulary = {_fold(w) for words, _ in phrases for w in words} | {_fold(w) for w in FUNCTION_WORDS}
    known = {_fold(n) for n in known_names} | {_fold(n) for n in callsigns.names}

    address_forms = dictionary_address_forms((*known_names, *callsigns.names))

    def protected(word):
        folded = _fold(word)
        # Порядкове «четвертий» — уже позивний разом із номером (дуб 4): число після нього
        # не його номер, тож слово не переписується.
        return (basic_normalize(word) in address_forms or folded in known or folded in vocabulary or basic_normalize(word) in NUMBER_WORDS
                or basic_normalize(word) in callsigns_module.ORDINALS)

    # Явний перелік: numbers — defaultdict, тож у ньому бувають порожні записи.
    # Позивний без номерів у словнику («вишня») може стояти перед цифрами повідомлення.
    numbered = {name: set(callsigns.numbers.get(name) or ()) for name in callsigns.names
                if callsigns.numbers.get(name) or name in callsigns.any_number or name in callsigns.standalone}
    aliases = callsigns_module.CALLSIGN_ALIASES
    replace = {}

    # 1. Позивний перед номером.
    for i, (_, _, word) in enumerate(tokens):
        if word.isdigit() or protected(word) or i + 1 >= len(tokens) or not tokens[i + 1][2].isdigit():
            continue
        heard = [tokens[i + 1][2]]
        if i + 2 < len(tokens) and tokens[i + 2][2].isdigit():
            heard.append(heard[0] + tokens[i + 2][2])      # «5, 6» -> 56
        folded, alias = _fold(word), aliases.get(basic_normalize(word))
        best = None
        for name, allowed in numbered.items():
            score = 100 if alias == name else fuzz.ratio(folded, name.translate(FOLD))
            own = name in callsigns.any_number or any(n in allowed for n in heard)
            if score >= (CALLSIGN_WITH_ITS_NUMBER if own else CALLSIGN_OTHER_NUMBER) and (
                    best is None or score > best[1]):
                best = (name, score)
        if best:
            replace[i] = _cased(best[0], word)

    # 2. Обороти: сусідні слова без цифр між ними, довші — першими.
    words = [i for i, token in enumerate(tokens) if not token[2].isdigit()]
    last = words[-1] if words else -1
    for parts, folded_phrase in phrases:
        n = len(parts)
        for k in range(len(words) - n + 1):
            idx = words[k:k + n]
            if idx[-1] - idx[0] != n - 1 or any(i in replace for i in idx):
                continue
            heard = [tokens[i][2] for i in idx]
            if all(_fold(h) == _fold(p) for h, p in zip(heard, parts)) or all(map(protected, heard)):
                continue
            if any(basic_normalize(h) in address_forms and _fold(h) != _fold(p) for h, p in zip(heard, parts)):
                continue
            if min(fuzz.ratio(_fold(h), _fold(p)) for h, p in zip(heard, parts)) < PHRASE_WORD:
                continue
            need = PHRASE_MULTI if n > 1 else (PHRASE_LAST if idx[-1] == last else PHRASE_MIDDLE)
            if fuzz.ratio(" ".join(_fold(h) for h in heard), folded_phrase) < need:
                continue
            for i, part in zip(idx, parts):
                if _fold(part) != _fold(tokens[i][2]):
                    replace[i] = _cased(part, tokens[i][2])

    out, pos = [], 0
    for i, (start, end, _) in enumerate(tokens):
        if i in replace:
            out += [text[pos:start], replace[i]]
            pos = end
    # Шаблони цілих фраз — після слів і зворотів: виправляють лише написання почутих слів.
    from shared.phrase_patterns import apply_patterns
    text = fix_double_over(apply_patterns("".join(out) + text[pos:], callsigns, known_names=known_names))
    callsign_names = {_fold(n) for n in callsigns.names}
    # Лише назви позивних: «і» перед назвою команди може бути справжнім.
    text = fix_i_before_callsign(text, lambda w: _fold(w) in callsign_names)
    text = fix_closing_turn(text, lambda w: protected(w) or _fold(w) in vocabulary,
                            lambda w: _fold(w) in callsign_names or basic_normalize(w) in address_forms)
    return strip_final_period(split_code_pairs(text))
