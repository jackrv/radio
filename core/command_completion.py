"""Погоджене відновлення пропущеного повтору лише за точним словником команд."""
import re

from core.callsigns import basic_normalize

_TOKENS = re.compile(r"[^\W\d_]+(?:[’'ʼ`-][^\W\d_]+)*|\d+")
_PROTECTED = re.compile(r'\b\d+[:./-]\d+\b|https?://', re.I)


def repeated_code_completions(text, callsigns, commands):
    """(початок, кінець, повний текст коду) для однієї почутої пари.

    Словник зберігає ABAB суцільно. Не відновлюємо за неточною назвою,
    іншим числом, дозволеним коротким кодом, іншим продовженням або колізією позивного.
    """
    if commands is None or _PROTECTED.search(text):
        return []
    tokens = list(_TOKENS.finditer(text))
    replacements = []
    for i, token in enumerate(tokens[:-1]):
        name = basic_normalize(token.group())
        if name not in commands.names or (callsigns is not None and name in callsigns.names):
            continue
        number = tokens[i+1]
        half = number.group()
        if not re.fullmatch(r'[0-9]{2}', half):
            continue
        if not re.fullmatch(r'[\s,]+', text[token.end():number.start()]):
            continue
        if number.end() < len(text) and (text[number.end()].isalnum() or text[number.end()] == '_'):
            continue
        if i+2 < len(tokens) and tokens[i+2].group().isdigit():
            continue
        # Не редагуємо процитований текст або частину вкладеного ідентифікатора.
        prefix = text[:token.start()]
        if (token.start() and (text[token.start()-1].isalnum() or text[token.start()-1] == '_')) or prefix.count('«') > prefix.count('»') or prefix.count('"') % 2:
            continue
        allowed = commands.numbers.get(name, set())
        full = half * 2
        if (name in commands.any_number or half in allowed or full not in allowed
                or any(code != full and code.startswith(half) for code in allowed)):
            continue
        replacements.append((number.start(), number.end(), f'{half} {half}'))
    return replacements


def restore_repeated_command_codes(text, callsigns, commands):
    for start, end, value in reversed(repeated_code_completions(text, callsigns, commands)):
        text = text[:start] + value + text[end:]
    return text
