from callsigns import Callsigns
from database import init_db, get_dictionary_lines


def load_dictionaries(db_path, log):
    """Загружает словари из SQLite и формирует prompt для Whisper."""

    init_db(db_path)

    callsigns = Callsigns.from_lines(
        get_dictionary_lines("callsigns", db_path)
    )

    commands = Callsigns.from_lines(
        get_dictionary_lines("commands", db_path)
    )

    watch = Callsigns.from_lines(
        get_dictionary_lines("telegram", db_path)
    )

    prompt = ", ".join(
        sorted(set(callsigns.names) | set(commands.names) | set(watch.names))
    )

    log.info(
        "Словари загружены из SQLite: позывные=%d, команды=%d, Telegram=%d",
        len(callsigns.names),
        len(commands.names),
        len(watch.names),
    )

    return callsigns, commands, watch, prompt
