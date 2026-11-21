"""Перевірка чернетки й збереження мають вказувати той самий рядок редактора."""

import pytest

from services.text_lists import check_list, read_lists, write_list


@pytest.mark.parametrize("kind", ["blacklist", "short_garbage"])
def test_disabled_and_blank_rows_do_not_shift_error_between_check_and_save(tmp_path, kind):
    db_path = tmp_path / "events.db"
    revision = read_lists(db_path)["revision"]
    entries = [
        {"raw_line": "вимкнений текст", "enabled": False},
        {"raw_line": "   ", "enabled": True},
        {"raw_line": "перший активний", "enabled": True},
        {"raw_line": "ще вимкнено", "enabled": False},
        {"raw_line": "синтетичний текст 42", "enabled": True},
    ]
    result = check_list(db_path, kind, entries)
    assert len(result["errors"]) == 1
    assert result["errors"][0].startswith("Рядок 4:")
    with pytest.raises(ValueError) as exc:
        write_list(db_path, kind, entries, revision)
    assert str(exc.value) == result["errors"][0]
    assert read_lists(db_path)["revision"] == revision
