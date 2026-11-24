"""Кандидати в шум і галюцинації: лише підказка з розмічених записів, без запису."""
from shared.noise_candidates import collect


def record(label, raw, status="ready", reference=""):
    return {"label": label, "status": status, "raw_text": raw, "reference": reference}


def test_repeated_non_speech_texts_become_candidates_with_safety_flags():
    records = [
        record("room", "Пау, пау, пау!"), record("noise", "пау пау пау", status="low_confidence"),
        record("room", "Дякую за перегляд, підписуйтесь на канал"), record("notification", "Дякую за перегляд, підписуйтесь на канал"),
        record("room", "Вишня 12"), record("room", "Вишня 12"),
        record("noise", "клен"), record("noise", "клен"),
        record("room", "добре"), record("room", "добре"), record("speech", "добре, прийом", reference="Дуб 14, добре, прийом"),
        record("room", "ага"), record("room", "ага"),
        record("room", "один раз"),
    ]
    items = {i["text"]: i for i in collect(records, {"ага"}, {"клен", "вишня"})}
    assert items["пау пау пау"]["list"] == "short_garbage" and items["пау пау пау"]["addable"]
    assert items["пау пау пау"]["count"] == 2 and items["пау пау пау"]["reached_text"] == 1
    assert items["дякую за перегляд підписуйтесь на канал"]["list"] == "blacklist"
    assert "has_number" in items["вишня 12"]["flags"] and "dictionary_word" in items["вишня 12"]["flags"]
    assert items["клен"]["flags"] == ["dictionary_word"]
    assert "in_speech" in items["добре"]["flags"], "слова справжнього мовлення не відкидаються"
    assert items["ага"]["flags"] == ["already_added"]
    assert "один раз" not in items, "одиничний текст — ще не закономірність"
    assert [i["addable"] for i in collect(records, {"ага"}, {"клен", "вишня"})][:2] == [True, True]


def test_word_close_to_a_dictionary_name_is_not_offered():
    records = [record("room", "кленн"), record("noise", "кленн")]
    items = {i["text"]: i for i in collect(records, set(), {"клен"})}
    assert items["кленн"]["flags"] == ["near_name"] and not items["кленн"]["addable"]
