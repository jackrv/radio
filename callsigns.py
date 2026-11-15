import re
import sys
from collections import defaultdict

from rapidfuzz import fuzz, process

# Початкові значення. Робочі списки зберігаються в SQLite (text_list_entries),
# редагуються в локальній панелі й застосовуються без перезапуску приймача.
# как могут произнести/написать -> как записано в списках (в нормализованном виде)
DEFAULT_ALIASES = {
    "пзрк": ["пе зе ер ка", "пэ зэ эр ка", "пзерка"],
    "вовк": ["волк"],
    "наган": [
        "на ган",
        "ган",
        "на han",
        "на ганд",
    ],
}

DEFAULT_CALLSIGN_ALIASES = {
    "улыбка": "липка",
}

# Поточні значення; замінюються цілком через configure_aliases().
ALIASES = {canon: list(variants) for canon, variants in DEFAULT_ALIASES.items()}
CALLSIGN_ALIASES = dict(DEFAULT_CALLSIGN_ALIASES)


def configure_aliases(aliases, callsign_aliases):
    """Атомарно замінює робочі алиаси (посилання на словник змінюється одним присвоєнням)."""
    global ALIASES, CALLSIGN_ALIASES
    ALIASES = {str(canon): [str(v) for v in variants] for canon, variants in aliases.items()}
    CALLSIGN_ALIASES = {str(heard): str(name) for heard, name in callsign_aliases.items()}

# числа словами (ru + uk); апострофы при нормализации удаляются
UNITS = {"нуль": 0, "ноль": 0, "один": 1, "одна": 1, "одне": 1, "два": 2, "дві": 2,
         "две": 2, "три": 3, "чотири": 4, "четыре": 4, "пять": 5, "шість": 6,
         "шесть": 6, "сім": 7, "семь": 7, "вісім": 8, "восемь": 8, "девять": 9}
TEENS = {"десять": 10, "одинадцять": 11, "одиннадцать": 11, "дванадцять": 12,
         "двенадцать": 12, "тринадцять": 13, "тринадцать": 13, "чотирнадцять": 14,
         "четырнадцать": 14, "пятнадцять": 15, "пятнадцать": 15, "шістнадцять": 16,
         "шестнадцать": 16, "сімнадцять": 17, "семнадцать": 17, "вісімнадцять": 18,
         "восемнадцать": 18, "девятнадцять": 19, "девятнадцать": 19}
TENS = {"двадцять": 20, "двадцать": 20, "тридцять": 30, "тридцать": 30, "сорок": 40,
        "пятдесят": 50, "пятьдесят": 50, "шістдесят": 60, "шестьдесят": 60,
        "сімдесят": 70, "семьдесят": 70, "вісімдесят": 80, "восемьдесят": 80,
        "девяносто": 90}
HUNDREDS = {"сто": 100, "двісті": 200, "двести": 200, "триста": 300, "чотириста": 400,
            "четыреста": 400, "пятсот": 500, "пятьсот": 500, "шістсот": 600,
            "шестьсот": 600, "сімсот": 700, "семьсот": 700, "вісімсот": 800,
            "восемьсот": 800, "девятсот": 900, "девятьсот": 900}
NUMBER_WORDS = set(UNITS) | set(TEENS) | set(TENS) | set(HUNDREDS)

# для нечёткого сравнения: сглаживаем различия укр/рус букв
FOLD = str.maketrans({"і": "и", "ї": "и", "ы": "и", "є": "е", "э": "е",
                      "ґ": "г", "ъ": None, "ь": None})


def basic_normalize(text: str) -> str:
    """Регістр, апострофи й пунктуація — без застосування алиасів."""
    text = text.lower().replace("ё", "е")
    text = re.sub(r"[’'ʼ`]", "", text)
    text = re.sub(r"[^\w\s]", " ", text)   # знаки препинания, дефисы
    return " ".join(text.split())


def normalize(text: str) -> str:
    text = basic_normalize(text)
    for canon, variants in ALIASES.items():
        for v in variants:
            text = re.sub(rf"\b{re.escape(v)}\b", canon, text)
    return text


def words_to_digits(tokens: list[str]) -> str:
    """['двісті','чотири'] -> '204'; ['тринадцять','тринадцять'] -> '1313'; ['7','3'] -> '73'"""
    groups, cur, last = [], 0, 0   # last: 1 сотни, 2 десятки, 3 10-19, 4 единицы

    def flush():
        nonlocal cur, last
        if last:
            groups.append(str(cur))
        cur, last = 0, 0

    for t in tokens:
        if t.isdigit():
            flush()
            groups.append(t)
            continue
        if t in HUNDREDS:
            lvl, val = 1, HUNDREDS[t]
        elif t in TENS:
            lvl, val = 2, TENS[t]
        elif t in TEENS:
            lvl, val = 3, TEENS[t]
        else:
            lvl, val = 4, UNITS[t]
        can_continue = (last == 1 and lvl in (2, 3, 4)) or (last == 2 and lvl == 4)
        if last and not can_continue:
            flush()
        cur += val
        last = lvl
    flush()
    return "".join(groups)


def to_items(text: str) -> list[tuple[str, str]]:
    """Преобразует текст в слова и отдельные числовые группы."""

    items = []

    # Запятые и точки с запятой разделяют числовые группы.
    # Внутри одной группы сохраняется преобразование чисел словами.
    for part in re.split(r"[,;]+", text):
        buf = []

        for t in normalize(part).split() + [""]:
            # Типичная ошибка Whisper: "шістдесят вістим" вместо "68".
            # Исправляем только после слова десятков.
            if t == "вістим" and buf and buf[-1] in TENS:
                t = "вісім"

            if t.isdigit() or t in NUMBER_WORDS:
                buf.append(t)
                continue

            if buf:
                items.append(("num", words_to_digits(buf)))
                buf = []

            if t:
                items.append(("word", t))

    return items


class Callsigns:
    """
    Формат рядків словника (усе, крім слів і номерів, необов’язкове):
        # коментар
        дуб: 204 205 300-310       слово з номерами й діапазонами
        дуб: *                     слово з будь-яким номером
        бук                        слово без номера
        дощ 1313                   «слово номер» (одна пара в рядку)
    """

    @classmethod
    def from_lines(cls, lines, threshold=80, standalone_threshold=90):
        """Создаёт словарь из строк, полученных, например, из SQLite."""
        obj = cls.__new__(cls)
        obj._load_lines(lines, threshold, standalone_threshold)
        return obj

    def _load_lines(self, lines, threshold, standalone_threshold):
        self.threshold = threshold
        self.standalone_threshold = standalone_threshold
        self.numbers = defaultdict(set)
        self.standalone = set()
        self.any_number = set()

        for line in lines:
            line = line.split("#")[0].strip()
            if not line:
                continue
            if ":" in line:
                head, tail = line.split(":", 1)
                name = normalize(head)
                for tok in re.split(r"[\s,;]+", tail.strip()):
                    if not tok:
                        continue
                    if tok == "*":
                        self.any_number.add(name)
                        continue
                    m = re.fullmatch(r"(\d+)-(\d+)", tok)
                    if m:
                        self.numbers[name].update(
                            str(n) for n in range(int(m[1]), int(m[2]) + 1))
                    else:
                        self.numbers[name].add(tok)
            else:
                parts = normalize(line).split()
                if len(parts) == 1:
                    self.standalone.add(parts[0])
                elif len(parts) >= 2:
                    self.numbers[parts[0]].add(parts[1])

        self.names = sorted(set(self.numbers) | self.standalone | self.any_number)
        self._folded = {n.translate(FOLD): n for n in self.names}
        self._keys = list(self._folded)


    def find(self, text: str) -> list[tuple[str, str | None, float]]:
        """Возвращает [(слово, номер или None, схожесть)] без повторов."""
        items = to_items(text)
        found, i = [], 0
        while i < len(items):
            kind, val = items[i]
            i += 1
            if kind != "word":
                continue
            alias = CALLSIGN_ALIASES.get(val)

            # Алиас веде до позивного з номерами (тоді потрібен дозволений номер далі)
            # або до позивного без номера (тоді діє на саме слово).
            if alias and (alias in self.numbers or alias in self.standalone or alias in self.any_number):
                best = (alias.translate(FOLD), 100, 0)
            else:
                best = process.extractOne(
                    val.translate(FOLD),
                    self._keys,
                    scorer=fuzz.ratio
                )
            if not best or best[1] < self.threshold:
                continue
            name, score = self._folded[best[0]], best[1]
            if i < len(items) and items[i][0] == "num":
                num = items[i][1]
                if num in self.numbers[name] or name in self.any_number:
                    found.append((name, num, score))
                    i += 1
                    continue
            if name in self.standalone and score >= self.standalone_threshold:
                found.append((name, None, score))

        seen, unique = set(), []
        for name, num, score in found:
            if (name, num) not in seen:
                seen.add((name, num))
                unique.append((name, num, score))
        return unique


if __name__ == "__main__":
    # Швидка перевірка розділу dictionaries.txt без мікрофона:
    #   python callsigns.py commands "арба п'ятнадцять п'ятнадцять"
    from config import DICTIONARIES_PATH
    from database import parse_dictionary_file

    sections = parse_dictionary_file(DICTIONARIES_PATH.read_text(encoding="utf-8-sig"))
    cs = Callsigns.from_lines([line for line, on in sections[sys.argv[1]] if on])
    for name, num, score in cs.find(" ".join(sys.argv[2:])):
        print(name, num or "", f"({score:.0f})")