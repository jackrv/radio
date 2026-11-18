"""М'яка підстановка шаблонів: позивні перед номером і обороти радіообміну."""

from unittest.mock import Mock

from core.callsigns import Callsigns, command_dictionary
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
    assert snap("Берега 5,6, 5316, приєм") == "Береза 5,6, 53 16, прийом"


def test_dictionary_and_phrase_words_are_not_rewritten():
    assert snap("берег 56, прийняв, прийом") == "берег 56, прийняв, прийом"
    assert snap("вишня, прийнято, прийом") == "вишня, прийнято, прийом"


def test_alias_and_callsign_without_numbers(monkeypatch):
    import core.callsigns as callsigns
    monkeypatch.setattr(callsigns, "CALLSIGN_ALIASES", {"бук": "клен"})
    assert snap("Бук 15, прийом") == "Клен 15, прийом"
    assert snap("Вишна 5316, прийом") == "Вишня 53 16, прийом"


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


def test_four_digit_codes_are_written_in_spoken_pairs():
    from core.template_text import split_code_pairs
    assert split_code_pairs("Клен 10, 4862, прийом") == "Клен 10, 48 62, прийом"
    assert split_code_pairs("злива 4242") == "злива 42 42"
    for kept in ("Клен 700", "1:29", "12.5034", "у 2026 році", "код1234", "12345"):
        assert split_code_pairs(kept) == kept


def test_callsign_number_split_by_whisper_is_joined_only_with_a_dictionary_number():
    from core.template_text import join_callsign_numbers
    cs = Callsigns.from_lines(["клен: 700 773", "дуб: 15"])
    assert join_callsign_numbers("Клен, 700, я клен, 700, 73, до роботи приступив, прийом", cs) == \
        "Клен 700, я клен 773, до роботи приступив, прийом"
    assert join_callsign_numbers("клен, 700, 74, прийом", cs) == "клен 700, 74, прийом", "774 немає у словнику"
    assert join_callsign_numbers("клен, 700, 72 59, прийом", cs) == "клен 700, 72 59, прийом", "далі код — не склеюємо"
    assert join_callsign_numbers("дуб, 16, прийом", cs) == "дуб, 16, прийом", "16 — не номер дуба"
    assert join_callsign_numbers("вишня, 700", cs) == "вишня, 700", "не позивний"


def test_whisper_final_period_is_dropped_but_inner_and_ellipsis_stay():
    from core.template_text import strip_final_period
    assert strip_final_period("Клен 15, прийом.") == "Клен 15, прийом"
    assert strip_final_period("Клен 15, прийом. Дуб 4, прийнято.") == "Клен 15, прийом. Дуб 4, прийнято"
    assert strip_final_period("Клен 15…") == "Клен 15…" and strip_final_period("Клен...") == "Клен..."


def test_double_over_is_usually_heard_as_received_over():
    from core.template_text import fix_double_over
    assert fix_double_over("Клен 48, прийом, прийом") == "Клен 48, прийнято, прийом"
    assert fix_double_over("Прийом прийом.") == "Прийнято прийом."
    assert fix_double_over("дуб, прийом, прийом / клен, прийом, прийом") == "дуб, прийнято, прийом / клен, прийнято, прийом"
    assert fix_double_over("Клен, прийом, прийом, прийом") == "Клен, прийом, прийом, прийом", "три поспіль — не чіпаємо"
    assert fix_double_over("прийом, прийнято") == "прийом, прийнято"
    assert fix_double_over("прийомка прийом") == "прийомка прийом"
