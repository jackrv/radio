"""Выделение кандидатов на словесные исправления.

Сопоставление идёт по токенам с защищёнными участками: числа и числительные
сворачиваются в числовые группы (как в ``callsigns.to_items``) и служат
якорями, а не удаляются. Изменения чисел никогда не становятся правилами, но
не мешают выделить независимые замены слов в другой части фразы.

Результат — кандидаты для голосования и пары для ручной проверки, а не
разрешённые глобальные правила.
"""

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from callsigns import FOLD, NUMBER_WORDS, TENS, to_items, words_to_digits


MAX_WORDS_PER_SIDE = 2
APOSTROPHES = "'’ʼ`"
_TOKEN = re.compile(r"[^\W_]+(?:[" + APOSTROPHES + r"][^\W_]+)*", re.UNICODE)


def tokenize(text: str) -> list[str]:
    """Разбивает текст на слова без применения алиасов позывных."""
    return re.findall(r"[^\W_]+", text.casefold(), re.UNICODE)


def number_groups(text: str) -> list[str]:
    """Возвращает числовые группы в существующем формате Radio 345."""
    return [
        value
        for kind, value in to_items(text)
        if kind == "num"
    ]


def contains_number(tokens: list[str]) -> bool:
    return any(
        token in NUMBER_WORDS
        or any(char.isdigit() for char in token)
        for token in tokens
    )


@dataclass
class _Item:
    kind: str           # "word" | "num"
    value: str          # слово (casefold) или цифры числовой группы
    tokens: list = field(default_factory=list)

    @property
    def key(self):
        return (self.kind, self.value)


def _plain(token):
    return re.sub("[" + APOSTROPHES + "]", "", token)


def _items(text: str) -> list[_Item]:
    """Слова и числовые группы с сохранением порядка.

    Как и ``to_items``: подряд идущие цифры и числительные без запятой или
    точки с запятой между ними образуют одну группу.
    """
    items, end = [], 0
    for match in _TOKEN.finditer(text or ""):
        token = match.group(0).casefold()
        plain = _plain(token)
        separator = text[end:match.start()]
        end = match.end()
        previous = items[-1] if items else None
        joinable = previous is not None and previous.kind == "num" and not re.search(r"[,;]", separator)
        if plain == "вістим" and joinable and previous.tokens and previous.tokens[-1] in TENS:
            plain = "вісім"
        if plain.isdigit() or plain in NUMBER_WORDS:
            if joinable and previous.tokens:
                previous.tokens.append(plain)
            else:
                items.append(_Item("num", "", [plain]))
            continue
        if any(char.isdigit() for char in token):
            # «68й», «2б»: захищений нечисловий фрагмент, ніколи не правило.
            items.append(_Item("num", token, []))
            continue
        items.append(_Item("word", token, [token]))
    for item in items:
        if item.kind == "num" and item.tokens:
            item.value = words_to_digits(item.tokens)
    return items


def similarity(left: str, right: str) -> float:
    """Схожість написання 0–100 з урахуванням укр/рус букв."""
    a, b = left.translate(FOLD).replace(" ", ""), right.translate(FOLD).replace(" ", "")
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio() * 100


PAIR_SIMILARITY = 60.0


@dataclass
class WordAnalysis:
    """Структурний розбір правки; класифікацію за словниками робить сховище."""

    pairs: list = field(default_factory=list)      # [(old, new)] — однозначні
    review: list = field(default_factory=list)     # [(old, new, reason)]
    excluded: list = field(default_factory=list)   # [(label, reason)]
    text_changed: bool = False
    format_only: bool = False


def _join(items):
    return " ".join(i.value for i in items)


def _word_block(old, new, analysis, *, before_changed_number=False):
    old_words = [i.value for i in old]
    new_words = [i.value for i in new]
    if not old_words and not new_words:
        return
    if not old_words or not new_words:
        label = " ".join(new_words) if new_words else " ".join(old_words)
        analysis.excluded.append((label, "inserted" if new_words else "deleted"))
        return
    label = f"{' '.join(old_words)} → {' '.join(new_words)}"

    def apostrophe(*texts):
        return any(set(APOSTROPHES) & set(text) for text in texts)

    if len(old_words) == len(new_words) == 2 and all(
            similarity(a, b) >= PAIR_SIMILARITY for a, b in zip(old_words, new_words)):
        pairs = [(a, b) for a, b in zip(old_words, new_words) if a != b]
    elif len(old_words) <= MAX_WORDS_PER_SIDE and len(new_words) <= MAX_WORDS_PER_SIDE:
        pairs = [(" ".join(old_words), " ".join(new_words))]
    elif len(old_words) == len(new_words):
        # Довге переписування: схожі позиційні пари лише як пропозиції для перевірки.
        similar = [(a, b) for a, b in zip(old_words, new_words)
                   if a != b and similarity(a, b) >= PAIR_SIMILARITY]
        for a, b in similar:
            if apostrophe(a, b):
                analysis.excluded.append((f"{a} → {b}", "apostrophe"))
            else:
                analysis.review.append((a, b, "long_rewrite"))
        if not similar:
            analysis.excluded.append((label, "long_rewrite"))
        return
    else:
        analysis.excluded.append((label, "long_rewrite"))
        return

    for pair in pairs:
        # Апостроф виключає лише свою пару, а не незалежну заміну поруч.
        if apostrophe(*pair):
            analysis.excluded.append((f"{pair[0]} → {pair[1]}", "apostrophe"))
            continue
        if before_changed_number:
            # Назва перед зміненим номером: імовірно інший адресат, а не написання.
            analysis.review.append((*pair, "with_number"))
        else:
            analysis.pairs.append(pair)


def _split_runs(items):
    runs, numbers, current = [], [], []
    for item in items:
        if item.kind == "num":
            runs.append(current)
            numbers.append(item)
            current = []
        else:
            current.append(item)
    runs.append(current)
    return runs, numbers


def analyze_word_changes(original: str, corrected: str) -> WordAnalysis:
    analysis = WordAnalysis()
    analysis.text_changed = (original or "").strip() != (corrected or "").strip()
    before, after = _items(original), _items(corrected)
    if not before or not after:
        return analysis
    if [i.key for i in before] == [i.key for i in after]:
        analysis.format_only = analysis.text_changed
        return analysis

    matcher = SequenceMatcher(None, [i.key for i in before], [i.key for i in after], autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        old, new = before[i1:i2], after[j1:j2]
        runs_old, nums_old = _split_runs(old)
        runs_new, nums_new = _split_runs(new)
        if nums_old or nums_new:
            analysis.excluded.append((
                f"{' '.join(n.value for n in nums_old) or '—'} → {' '.join(n.value for n in nums_new) or '—'}",
                "number"))
        if len(nums_old) == len(nums_new):
            for index, (a, b) in enumerate(zip(runs_old, runs_new)):
                _word_block(a, b, analysis, before_changed_number=index < len(nums_old))
        else:
            words_old = [i for i in old if i.kind == "word"]
            words_new = [i for i in new if i.kind == "word"]
            if words_old or words_new:
                analysis.excluded.append((f"{_join(words_old) or '—'} → {_join(words_new) or '—'}", "number_structure"))

    # Одне слово, виправлене по-різному в одній фразі, — суперечність.
    variants = {}
    for old, new, *_ in analysis.pairs + analysis.review:
        variants.setdefault(old, set()).add(new)
    conflicted = {old for old, values in variants.items() if len(values) > 1}
    for old in sorted(conflicted):
        analysis.excluded.append((f"{old} → {' / '.join(sorted(variants[old]))}", "conflict"))
    seen = set()
    analysis.pairs = [p for p in analysis.pairs if p[0] not in conflicted and not (p in seen or seen.add(p))]

    seen_review = set()
    analysis.review = [r for r in analysis.review
                       if r[0] not in conflicted and r[:2] not in seen and not (r[:2] in seen_review or seen_review.add(r[:2]))]

    # Те саме написання лишилося невиправленим в іншому місці фрази —
    # це суперечить загальному правилу: лише ручна перевірка.
    remaining = [i.value for i in after if i.kind == "word"]

    def leftover(old):
        words = old.split()
        return any(remaining[k:k + len(words)] == words for k in range(len(remaining) - len(words) + 1))

    moved = [p for p in analysis.pairs if leftover(p[0])]
    analysis.pairs = [p for p in analysis.pairs if p not in moved]
    analysis.review = [(o, n, "leftover" if leftover(o) else r) for o, n, r in analysis.review]
    analysis.review += [(o, n, "leftover") for o, n in moved]
    return analysis


def extract_word_changes(
    original: str,
    corrected: str,
) -> list[tuple[str, str]]:
    """Однозначні короткі заміни (без урахування словників).

    Це кандидати на навчання, а не дозволені глобальні правила.
    """
    return list(analyze_word_changes(original, corrected).pairs)
