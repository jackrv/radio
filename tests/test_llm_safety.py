from core.llm_safety import is_conservative_llm_rewrite


def test_rejects_direction_change():
    assert not is_conservative_llm_rewrite(
        "Рухаємося на північ",
        "Рухаємося на південь",
    )


def test_rejects_content_change():
    assert not is_conservative_llm_rewrite(
        "Липка 700, все чисто",
        "Липка 700, все тихо",
    )


def test_rejects_location_change():
    assert not is_conservative_llm_rewrite(
        "Липка 700, я тут",
        "Липка 700, я там",
    )


def test_allows_case_change():
    assert is_conservative_llm_rewrite(
        "Липка 700",
        "липка 700",
    )


def test_allows_ukrainian_letter_variant():
    assert is_conservative_llm_rewrite(
        "Лира 68",
        "Ліра 68",
    )


def test_rejects_unconfirmed_complex_rewrite():
    assert not is_conservative_llm_rewrite(
        "Липка 700, я на свези",
        "Липка 700, я на зв'язку",
    )


def test_rejects_inserted_words():
    assert not is_conservative_llm_rewrite(
        "Липка 700",
        "Липка 700, я на зв'язку",
    )
