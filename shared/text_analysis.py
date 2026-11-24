"""Спільний аналіз тексту приймача й лабораторії без SQLite, Telegram та аудіо."""

from dataclasses import dataclass

from shared.analyzer import canonical_text, confident_roles, is_bare_callsign, is_short_garbage, split_roles
from shared.template_text import template_text
from shared.transcript_format import tidy_text
from shared.dictionary_blocks import constrain_dictionary_text


@dataclass
class TextAnalysis:
    text: str
    callsigns: list
    commands: list
    watch: list
    recipient: list
    sender: list
    rejected: str | None = None
    partial_commands: frozenset = frozenset()   # (назва, код) команд, чий код почуто наполовину


def analyze_text(text, callsigns, commands, watch, short_garbage, log):
    """Ті самі правила й порядок, що в EventProcessor; побічних дій немає."""
    original = text
    try:
        text, dictionary_changes = constrain_dictionary_text(text,callsigns,commands)
        if dictionary_changes:
            from shared.recording_trace import note
            note(dictionary_corrections=dictionary_changes)
        text = canonical_text(text, (commands, callsigns, watch))
        text = template_text(text, callsigns, (*commands.names, *watch.names))
    except Exception:
        log.exception("Не вдалося застосувати автокорекцію; обробляю текст без неї")
        text = original
    cs_found = callsigns.find(text)
    cmd_found = commands.find(text)
    watch_found = watch.find(text)
    to_found, from_found = confident_roles(text, *split_roles(text, callsigns), callsigns)
    rejected = None
    if is_short_garbage(text, short_garbage) and not cs_found and not cmd_found and not watch_found:
        rejected = "short_garbage"
    elif not cmd_found and not watch_found and is_bare_callsign(text, cs_found, callsigns):
        rejected = "bare_callsign"
    try:
        # Лише позначка для шапки: збій тут не повинен зупинити подію чи сповіщення.
        partial = frozenset((m.name, m.number) for m in commands.matches(text) if getattr(m, "partial", False))
    except Exception:
        partial = frozenset()
    # Оформлення не повинно змінювати вже визначені метадані/одержувачів.
    try:
        text = tidy_text(text, callsigns, commands)
    except Exception:
        log.exception("Не вдалося оформити текст; залишаю попередній результат")
    return TextAnalysis(text, cs_found, cmd_found, watch_found, to_found, from_found, rejected, partial)
