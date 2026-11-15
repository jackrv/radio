from callsigns import Callsigns
from services.alias_candidates import collect

LINES = ["вишня: 210 300", "клен: 300 301", "ясень: *", "береза: 5", "дуб"]


def rec(i, reference, raw):
    return {"id": f"r{i}", "reference": reference, "raw_text": raw}


def by_heard(items):
    return {(c["heard"], c["callsign"]): c for c in items}


def test_candidates_come_from_same_number_and_carry_flags():
    cs = Callsigns.from_lines(LINES)
    names = set(cs.names) | {"злива"}
    records = [
        rec(1, "Вишня 210, я клен 301, прийом", "Гуща 210, я клен 301, прийом"),
        rec(2, "Вишня 210, прийнято", "гуща 210 прийнято"),
        rec(3, "Клен 300, вишня 300, вишня 210", "вишна 210, лапа 300"),  # 300 у двох позивних — пропуск
        rec(4, "Клен 301", "злива 301"),
        rec(5, "Береза 5, прийом", "я 5 прийом"),
        rec(6, "Ясень 77", "ясин 77"),
        rec(7, "Вишня 210", ""),                              # мовлення без тексту — пропуск
    ]
    items = by_heard(collect(records, cs, names, {"ясин": "ясень"}))
    gusha = items[("гуща", "вишня")]
    assert gusha["count"] == 2 and gusha["addable"] and gusha["flags"] == [] and gusha["records"] == ["r1", "r2"]
    assert "already_matched" in items[("вишна", "вишня")]["flags"]
    assert not any(h == "лапа" for h, _ in items), "номер у двох позивних не дає кандидата"
    # «злива» — слово іншого словника: алиас до позивного з номерами допустимий.
    assert items[("злива", "клен")]["flags"] == ["other_dictionary"] and items[("злива", "клен")]["addable"]
    assert "function_word" in items[("я", "береза")]["flags"] and not items[("я", "береза")]["addable"]
    assert "already_added" in items[("ясин", "ясень")]["flags"]
    assert list(items)[0] == ("гуща", "вишня"), "придатні й часті — першими"


def test_unknown_number_and_ambiguous_target_are_marked():
    cs = Callsigns.from_lines(LINES)
    records = [rec(1, "Вишня 999", "гуща 999"), rec(2, "Клен 301", "гуща 301")]
    items = by_heard(collect(records, cs, set(cs.names), {}))
    assert "number_unknown" in items[("гуща", "вишня")]["flags"]
    assert all("ambiguous" in c["flags"] for c in items.values())


def test_callsign_word_is_blocked_and_standalone_callsign_gets_candidates():
    cs = Callsigns.from_lines(LINES)
    records = [rec(1, "Клен 301", "вишня 301"),
               rec(2, "дуб, прийнято, прийом", "дубок, прийнято, прийом"),
               rec(3, "дуб прийом", "дуб прийом")]
    items = by_heard(collect(records, cs, set(cs.names), {}))
    assert items[("вишня", "клен")]["flags"] == ["dictionary_word"] and not items[("вишня", "клен")]["addable"]
    dubok = items[("дубок", "дуб")]
    assert dubok["count"] == 1 and "no_number" in dubok["flags"] and dubok["addable"]
    assert ("прийнято", "дуб") not in items, "слова з правильного тексту не стають кандидатами"
