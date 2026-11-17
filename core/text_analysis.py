"""Спільний аналіз тексту приймача й лабораторії без SQLite, Telegram та аудіо."""

from dataclasses import dataclass

from core.analyzer import canonical_text, is_bare_callsign, is_short_garbage, split_roles
from core.template_text import template_text


@dataclass
class TextAnalysis:
    text: str
    callsigns: list
    commands: list
    watch: list
    recipient: list
    sender: list
    rejected: str | None = None


def analyze_text(text, callsigns, commands, watch, short_garbage, log):
    """Ті самі правила й порядок, що в EventProcessor; побічних дій немає."""
    original = text
    try:
        text = canonical_text(text, (commands, callsigns, watch))
        text = template_text(text, callsigns, (*commands.names, *watch.names))
    except Exception:
        log.exception("Не вдалося застосувати автокорекцію; обробляю текст без неї")
        text = original
    cs_found = callsigns.find(text)
    cmd_found = commands.find(text)
    watch_found = watch.find(text)
    to_found, from_found = split_roles(text, callsigns)
    rejected = None
    if is_short_garbage(text, short_garbage) and not cs_found and not cmd_found and not watch_found:
        rejected = "short_garbage"
    elif not cmd_found and not watch_found and is_bare_callsign(text, cs_found, callsigns):
        rejected = "bare_callsign"
    return TextAnalysis(text, cs_found, cmd_found, watch_found, to_found, from_found, rejected)
