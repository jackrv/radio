"""М'яка підстановка шаблонів у текст сповіщення.

Whisper часто чує позивні й обороти радіообміну з однією-двома хибними літерами:
«Привой 91», «посилка отримав», «приєм». Тут такі слова пишуться так, як у
словнику позивних і в переліку оборотів (ASR_TEMPLATES.md). Правила навмисно
обережні:

- номери не змінюються ніколи, позивний перед номером замінюється лише тоді,
  коли номер є в словнику цього позивного (або слово дуже схоже на назву);
- слово, яке саме є назвою зі словників чи словом обороту, не замінюється:
  «прийняв» не стає «прийнято», назва команди не стає позивним;
- оборот замінюється, лише якщо кожне його слово схоже на почуте;
- «прийом» пишеться замість схожого слова лише в кінці тексту;
- незрозумілі шматки лишаються як почуті.

Результат — лише для показу. Подія в базі зберігає почутий текст.
"""

import re

from rapidfuzz import fuzz

import callsigns as callsigns_module
from callsigns import FOLD, NUMBER_WORDS, basic_normalize

# Обороти з підтверджених оператором шаблонів (ASR_TEMPLATES.md, розділ 2).
PHRASES = (
    "прийом", "прийнято", "прийняв", "посилку отримав", "на звʼязку", "на звʼязок",
    "перевірка звʼязку", "до роботи приступив", "роботу закінчив", "роботу розпочав",
    "чую та розбираю", "чую і розбираю", "повторіть голос не пройшов",
)
FUNCTION_WORDS = ("я", "на", "до", "як", "та", "і", "й", "не", "добре", "вас", "з")

# Пороги схожості (rapidfuzz.ratio, 0–100), підібрані на архіві 16.11.
CALLSIGN_WITH_ITS_NUMBER = 60    # «Берега 56» -> «Береза 56», якщо 56 є в словнику берези
CALLSIGN_OTHER_NUMBER = 80       # «бериза 99» -> «береза 99»: номер лишається почутим
PHRASE_WORD = 55                 # кожне слово обороту окремо
PHRASE_MULTI = 75                # оборот із кількох слів загалом
PHRASE_LAST = 70                 # одне слово в кінці тексту («приєм» -> «прийом»)
PHRASE_MIDDLE = 82               # одне слово всередині тексту

_TOKEN = re.compile(r"[^\W\d_]+(?:[’'ʼ`-][^\W\d_]+)*|\d+")


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
    tokens = [(m.start(), m.end(), m.group()) for m in _TOKEN.finditer(text)]
    if not tokens:
        return text
    phrases = _phrases()
    vocabulary = {_fold(w) for words, _ in phrases for w in words} | {_fold(w) for w in FUNCTION_WORDS}
    known = {_fold(n) for n in known_names} | {_fold(n) for n in callsigns.names}

    def protected(word):
        folded = _fold(word)
        return folded in known or folded in vocabulary or basic_normalize(word) in NUMBER_WORDS

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
    return "".join(out) + text[pos:]
