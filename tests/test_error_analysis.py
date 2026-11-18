from services.error_analysis import analyse, record_errors

NAMES = {"клен", "дуб", "ясень"}


def test_kinds_of_differences_are_separated():
    assert record_errors("Клен 48, прийом", "Клен 48, прийом.", NAMES) == (set(), [])
    found, pairs = record_errors("Клен 48, я Дуб 7, прийом", "Клей 48, я Дуб 9, прийом", NAMES)
    assert found == {"callsigns", "numbers"} and ("клей", "клен") in pairs and ("9", "7") in pairs
    assert record_errors("Дуб 7, прийнято", "Дуб 7, прийом", NAMES)[0] == {"words"}
    assert record_errors("Дуб 7, прийом", "Дуб 7, так, прийом", NAMES)[0] == {"inserted", "words"}
    assert record_errors("Дуб 7, так, прийом", "Дуб 7, прийом", NAMES)[0] == {"deleted", "words"}
    assert record_errors("Дуб 7", "", NAMES)[0] == {"missed"}


def test_only_digit_grouping_is_its_own_kind():
    found, _ = record_errors("злива 48 62", "злива 4862", NAMES)
    assert found == {"regroup"}, "числа ті самі — лише інакше згруповані"
    found, _ = record_errors("злива 48 62", "злива 4863", NAMES)
    assert "numbers" in found and "regroup" not in found


def test_analysis_counts_records_processing_and_top_substitutions():
    records = [
        {"id": "a" * 32, "reference": "Клен 48, прийом", "raw_text": "клей 48 прийом", "final_text": "Клен 48, прийом"},
        {"id": "b" * 32, "reference": "Клен 50, прийом", "raw_text": "клей 50 прийом", "final_text": "клей 50 прийом"},
        {"id": "c" * 32, "reference": "Клен 51, прийом", "raw_text": "Клен 51 прийом", "final_text": "клей 51 прийом"},
    ]
    result = analyse(records, NAMES)
    assert result["records"] == 3 and result["with_edits"] == 2
    assert result["categories"]["callsigns"]["count"] == 2
    assert result["processing"] == {"better": 1, "worse": 1, "better_ids": ["a" * 32], "worse_ids": ["c" * 32]}
    assert result["substitutions"][0] == {"from": "клей", "to": "клен", "count": 2, "kind": "callsign", "ids": ["b" * 32, "c" * 32]}
