"""М'яка підстановка шаблонів: позивні перед номером і обороти радіообміну."""

from unittest.mock import Mock

from callsigns import Callsigns, command_dictionary
from core.event_processor import EventProcessor
from core.template_text import template_text

CALLSIGNS = Callsigns.from_lines(["береза: 31 56", "клен: 15", "вишня"])
COMMANDS = command_dictionary(["берег 4242"])
KNOWN = COMMANDS.names


def snap(text):
    return template_text(text, CALLSIGNS, KNOWN)


def test_callsign_and_phrases_are_written_as_in_templates():
    assert snap("Берега 56, посилка отриму, приєм") == "Береза 56, посилку отримав, прийом"


def test_callsign_threshold_depends_on_number():
    assert snap("береш 31, прийом") == "береза 31, прийом"      # 73 %, але 31 — номер берези
    assert snap("береш 99, прийом") == "береш 99, прийом"       # 73 % з чужим номером — мало
    assert snap("бериза 99, прийом") == "береза 99, прийом"     # 83 %: номер лишається почутим


def test_numbers_are_never_changed():
    assert snap("Берега 5,6, 5316, приєм") == "Береза 5,6, 5316, прийом"


def test_dictionary_and_phrase_words_are_not_rewritten():
    assert snap("берег 56, прийняв, прийом") == "берег 56, прийняв, прийом"
    assert snap("вишня, прийнято, прийом") == "вишня, прийнято, прийом"


def test_alias_and_callsign_without_numbers(monkeypatch):
    import callsigns
    monkeypatch.setattr(callsigns, "CALLSIGN_ALIASES", {"бук": "клен"})
    assert snap("Бук 15, прийом") == "Клен 15, прийом"
    assert snap("Вишна 5316, прийом") == "Вишня 5316, прийом"


def test_unclear_words_stay_as_heard():
    for text in ("посилку, треба, прийом",          # «треба» не схоже на «отримав»
                 "як приймаю мою роботу",           # «прийом» лише в кінці тексту
                 "ресора 56",                       # не схоже на позивний
                 "клін, приємно"):                  # без номера позивний не чіпаємо
        assert snap(text) == text


def test_only_last_word_becomes_end_of_message():
    assert snap("Береза 56, приймаю") == "Береза 56, прийом"
    assert snap("приймаю, береза 56") == "приймаю, береза 56"


def test_event_and_notification_get_templates_heard_text_is_kept_as_raw():
    telegram, save = Mock(), Mock(return_value=7)
    heard = "Берега 56, посилка отриму, приєм"
    EventProcessor(
        callsigns=CALLSIGNS, commands=COMMANDS, watch=Callsigns.from_lines([]), short_garbage=set(),
        save_event=save, telegram_service=telegram, fresh=lambda _: True,
        select_recipients=lambda wants: [{"chat_id": 1, "role": "admin", "mode": "all"}],
        db_path="unused-test.db", log_callsigns_only=True, log=Mock(),
    ).process(heard, b"\0\0")
    assert save.call_args.args[4] == "Береза 56, посилку отримав, прийом"
    assert save.call_args.kwargs["raw_text"] == heard
    assert telegram.notify.call_args.args[3] == "Береза 56, посилку отримав, прийом"
