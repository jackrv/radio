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

    if not callsigns.names:
        message = (
            "Словник позивних порожній. Запуск Radio 345 зупинено. "
            "Перевірте dictionary_entries у SQLite "
            "(dictionary_type='callsigns', enabled=1)."
        )
        log.error(message)
        raise RuntimeError(message)

    if not commands.names:
        log.warning(
            "Словник команд порожній: виявлення команд недоступне"
        )

    if not watch.names:
        log.warning(
            "Словник Telegram порожній: список спостереження недоступний"
        )

    prompt = ", ".join(
        sorted(set(callsigns.names) | set(commands.names) | set(watch.names))
    )

    log.info(
        "Словники завантажено зі SQLite: позивні=%d, команди=%d, Telegram=%d",
        len(callsigns.names),
        len(commands.names),
        len(watch.names),
    )

    return callsigns, commands, watch, prompt
