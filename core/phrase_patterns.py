"""Шаблони цілих фраз: «{П}, {П}, я {П}, перевірка звʼязку, прийом».

Шаблон лише виправляє написання вже розпізнаних слів — нічого не додає й не
прибирає, числа не змінює ніколи. Фраза (частина тексту між . ! ? ; /) має
повністю пояснюватися шаблоном: кожне почуте слово стає або словом шаблону,
або позивним на місці {П}. Тоді:

- слова шаблону пишуться як у шаблоні («перевирка звязку» → «перевірка звʼязку»);
- слово на місці позивного пишеться як позивний зі словника, якщо почутий номер
  є в словнику саме цього позивного (або позивний без номера чи «*») і слово
  схоже на нього; назви зі словників не переписуються іншими назвами.

Місця: {П} — позивний (слово й необов’язковий номер), {Ч} — число чи код,
{К} — команда (слово й необов’язковий номер).
"""

import re

from rapidfuzz import fuzz

import core.callsigns as callsigns_module
from core.callsigns import FOLD, basic_normalize, dictionary_address_forms

SLOTS = {"п": "P", "ч": "N", "к": "C"}
FIXED_WORD_MIN = 62          # кожне слово шаблону — щонайменше настільки схоже на почуте
SLOT_CALLSIGN_MIN = 50       # слово на місці позивного → позивний із дозволеним почутим номером
SLOT_STANDALONE_MIN = 80     # без номера перевірити нічим: лише майже точний збіг із позивним
# Службові слова, почуті правильно, не замінюються іншими: «прийом» ≠ «прийнято».
PROTECTED_WORDS = {"прийом", "прийнято", "прийняв", "прийняла", "так", "ні", "добре", "я"}
MAX_COST = 0.3               # середня «ціна» відповідності на елемент шаблону
SKIPPED_WORDS_MAX = 1        # скільки слів шаблону може бракувати в почутому

_TOKEN = re.compile(r"[^\W\d_]+(?:[’'ʼ`-][^\W\d_]+)*|\d+")
_SEGMENT = re.compile(r"[^.!?;/]+")
_ELEMENT = re.compile(r"\{([^}]+)\}|[^\W\d_]+(?:[’'ʼ`-][^\W\d_]+)*|\d+")

PATTERNS = ()


def _fold(word):
    return basic_normalize(word).translate(FOLD)


def parse_pattern(line):
    """«{П}, я {П}, прийом» → [("P",), ("W", "я"), ("P",), ("W", "прийом")]; ValueError з поясненням."""
    elements = []
    for match in _ELEMENT.finditer(line):
        if match.group(1) is not None:
            slot = SLOTS.get(match.group(1).strip().lower())
            if slot is None:
                raise ValueError(f"невідоме місце «{{{match.group(1)}}}»: є {{П}}, {{Ч}}, {{К}}")
            elements.append((slot,))
        elif match.group().isdigit():
            raise ValueError("числа в шаблоні — лише місцем {Ч}")
        else:
            # Слово пишеться як у шаблоні (з апострофом); порівняння — без апострофа (_fold).
            elements.append(("W", match.group().lower().replace("'", "ʼ").replace("’", "ʼ").replace("`", "ʼ")))
    if not any(e[0] == "W" for e in elements):
        raise ValueError("потрібне хоча б одне слово, крім місць")
    return elements


def configure_patterns(lines):
    """Застосувати шаблони зі SQLite; непридатні рядки пропускаються (їх не збереже панель)."""
    global PATTERNS
    parsed = []
    for line in lines:
        try:
            parsed.append((line, parse_pattern(line)))
        except ValueError:
            continue
    PATTERNS = tuple(parsed)


def _expand(elements):
    """{П} → слово позивного + необов’язкові числа; {Ч} → числа; {К} → слово команди + числа."""
    out = []
    for element in elements:
        if element[0] == "P":
            out += [("PW",), ("NUMS",)]
        elif element[0] == "C":
            out += [("CW",), ("NUMS",)]
        elif element[0] == "N":
            out += [("NUMS",)]
        else:
            out.append(element)
    return out


def _best_callsign(word, numbers, callsigns):
    """Позивний для слова на місці {П}: лише з дозволеним почутим номером; (назва, схожість) або None."""
    heard = basic_normalize(word)
    alias = callsigns_module.CALLSIGN_ALIASES.get(heard)
    best = None
    for name in callsigns.names:
        allowed = set(callsigns.numbers.get(name) or ())
        if numbers:
            fits = name in callsigns.any_number or any(n in allowed for n in numbers)
        else:
            fits = name in callsigns.standalone or not allowed
        if not fits:
            continue
        score = 100 if alias == name else fuzz.ratio(_fold(word), name.translate(FOLD))
        if best is None or score > best[1]:
            best = (name, score)
    return best


COMMAND_SLOT_MIN = 75        # слово на місці {К} — команда зі словника або дуже схоже на неї


def _align(elements, tokens, callsigns, names, commands=frozenset()):
    """Вирівнювання шаблону з токенами фрази (динамічне програмування). (ціна, пари) або None."""
    n, m = len(elements), len(tokens)
    inf = float("inf")
    cost = [[inf] * (m + 1) for _ in range(n + 1)]
    back = [[None] * (m + 1) for _ in range(n + 1)]
    cost[0][0] = 0.0
    is_num = [t.isdigit() for t in tokens]

    def word_cost(element, token, j):
        kind = element[0]
        if kind == "W":
            if _fold(token) != _fold(element[1]) and basic_normalize(token) in PROTECTED_WORDS:
                return inf
            score = fuzz.ratio(_fold(token), _fold(element[1]))
            return (100 - score) / 100 if score >= FIXED_WORD_MIN else inf
        if kind == "PW":
            if _fold(token) in names:
                return 0.0
            numbers = []
            k = j + 1
            while k < m and is_num[k]:
                numbers.append(tokens[k]); k += 1
            joined = ["".join(numbers[:2])] if len(numbers) >= 2 else []
            best = _best_callsign(token, numbers + joined, callsigns)
            need = SLOT_CALLSIGN_MIN if numbers else SLOT_STANDALONE_MIN
            return 0.15 if best and best[1] >= need else 0.6
        if kind == "CW":
            folded = _fold(token)
            if folded in commands:
                return 0.0
            best = max((fuzz.ratio(folded, c) for c in commands), default=0)
            return 0.2 if best >= COMMAND_SLOT_MIN else inf
        return inf

    for i in range(n + 1):
        for j in range(m + 1):
            here = cost[i][j]
            if here == inf:
                continue
            if i < n:
                kind = elements[i][0]
                skip = 0.0 if kind == "NUMS" else (0.8 if kind == "W" else 1.0)
                if here + skip < cost[i + 1][j]:
                    cost[i + 1][j], back[i + 1][j] = here + skip, (i, j, "skip_e")
            if i < n and j < m:
                kind = elements[i][0]
                if kind == "NUMS":
                    if is_num[j] and here < cost[i][j + 1]:      # число лишається на тому ж місці шаблону
                        cost[i][j + 1], back[i][j + 1] = here, (i, j, "num")
                elif not is_num[j]:
                    step = word_cost(elements[i], tokens[j], j)
                    if here + step < cost[i + 1][j + 1]:
                        cost[i + 1][j + 1], back[i + 1][j + 1] = here + step, (i, j, "match")
    total = cost[n][m]
    if total == inf:
        return None
    pairs, skipped, i, j = [], 0, n, m
    while (i, j) != (0, 0):
        pi, pj, op = back[i][j]
        if op == "match":
            pairs.append((pi, pj))
        elif op == "skip_e" and elements[pi][0] == "W":
            skipped += 1
        elif op == "skip_e" and elements[pi][0] in ("PW", "CW"):
            return None                                    # бракує позивного чи команди — не цей шаблон
        i, j = pi, pj
    weight = sum(1 for e in elements if e[0] != "NUMS")
    if skipped > SKIPPED_WORDS_MAX or total / max(1, weight) > MAX_COST:
        return None
    return total / max(1, weight), pairs[::-1]


def _cased(new, old):
    return new[:1].upper() + new[1:] if old[:1].isupper() else new


def apply_patterns(text, callsigns, patterns=None, known_names=()):
    """Текст, у якому фрази, що повністю відповідають шаблону, написано як у шаблоні.

    ``known_names`` — назви команд і списку спостереження: їх так само не переписуємо.
    """
    patterns = PATTERNS if patterns is None else patterns
    if not patterns or not text:
        return text
    names = {name.translate(FOLD) for name in (*callsigns.names, *known_names)}
    address_forms = dictionary_address_forms((*callsigns.names, *known_names))
    commands = frozenset(_fold(name) for name in known_names)
    out, last = [], 0
    for segment in _SEGMENT.finditer(text):
        found = [(m.start() + segment.start(), m.end() + segment.start(), m.group()) for m in _TOKEN.finditer(segment.group())]
        if not found:
            continue
        tokens = [t[2] for t in found]
        best = None
        for _, elements in patterns:
            expanded = _expand(elements)
            result = _align(expanded, tokens, callsigns, names, commands)
            if result and (best is None or result[0] < best[0]):
                best = (result[0], expanded, result[1])
        if best is None:
            continue
        _, expanded, pairs = best
        for ei, tj in pairs:
            start, end, token = found[tj]
            # Попередня словесна корекція захищає «Клену»/«Кленові»;
            # цілий шаблон не повинен потім прибирати законне закінчення.
            if basic_normalize(token) in address_forms:
                continue
            kind = expanded[ei][0]
            new = token
            if kind == "W" and _fold(token) != _fold(expanded[ei][1]):
                new = _cased(expanded[ei][1], token)
            elif kind == "PW" and _fold(token) not in names:
                numbers = []
                k = tj + 1
                while k < len(tokens) and tokens[k].isdigit():
                    numbers.append(tokens[k]); k += 1
                joined = ["".join(numbers[:2])] if len(numbers) >= 2 else []
                choice = _best_callsign(token, numbers + joined, callsigns)
                need = SLOT_CALLSIGN_MIN if numbers else SLOT_STANDALONE_MIN
                if choice and choice[1] >= need:
                    new = _cased(choice[0], token)
            if new != token:
                out += [text[last:start], new]
                last = end
    return "".join(out) + text[last:]
