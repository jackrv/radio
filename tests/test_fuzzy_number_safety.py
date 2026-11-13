import pytest

from services.event_store import (
    init_db,
    save_correction,
    apply_learned_corrections,
)


def make_db(tmp_path, original, corrected):
    path = str(tmp_path / "test_events.db")
    init_db(path)
    save_correction(original, corrected, db_path=path)
    return path


@pytest.mark.parametrize(
    ("incoming", "learned"),
    [
        ("липка 709", "липка 700"),
        ("липка 404", "липка 405"),
        ("наган 101", "наган 107"),
        ("прибой 118", "прибой 119"),
    ],
)
def test_fuzzy_does_not_change_numbers(
    tmp_path, incoming, learned
):
    path = make_db(tmp_path, learned, learned + ", прийом")

    result = apply_learned_corrections(
        incoming, db_path=path, fuzzy_score=86
    )

    assert result == (incoming, False)


def test_fuzzy_checks_corrected_numbers_too(tmp_path):
    path = make_db(
        tmp_path,
        "Липка 700 прийма",
        "Липка 701 прийом",
    )

    incoming = "Липка 700 прийм"

    assert apply_learned_corrections(
        incoming, db_path=path
    ) == (incoming, False)


def test_exact_manual_number_correction_is_allowed(tmp_path):
    path = make_db(
        tmp_path,
        "Липка 709",
        "Липка 700",
    )

    assert apply_learned_corrections(
        "Липка 709", db_path=path
    ) == ("Липка 700", True)


def test_fuzzy_word_correction_still_works(tmp_path):
    path = make_db(
        tmp_path,
        "Липка 700 прийма",
        "Липка 700 прийом",
    )

    assert apply_learned_corrections(
        "Липка 700 прийм", db_path=path
    ) == ("Липка 700 прийом", True)


def test_fuzzy_preserves_separate_number_groups(tmp_path):
    path = make_db(
        tmp_path,
        "Ліра 68, 37, 49, прийом",
        "Ліра 68, 37, 49, прийом",
    )

    incoming = "Ліра 68, 37, 40, прийом"

    assert apply_learned_corrections(
        incoming, db_path=path
    ) == (incoming, False)
