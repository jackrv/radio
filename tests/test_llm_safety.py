from shared.llm_safety import is_conservative_llm_rewrite


def test_rejects_direction_change():
    assert not is_conservative_llm_rewrite(
        "Рухаємося на північ",
        "Рухаємося на південь",
    )


def test_rejects_content_change():
    assert not is_conservative_llm_rewrite(
        "Вишня 700, все чисто",
        "Вишня 700, все тихо",
    )


def test_rejects_location_change():
    assert not is_conservative_llm_rewrite(
        "Вишня 700, я тут",
        "Вишня 700, я там",
    )


def test_allows_case_change():
    assert is_conservative_llm_rewrite(
        "Вишня 700",
        "вишня 700",
    )


def test_allows_ukrainian_letter_variant():
    assert is_conservative_llm_rewrite(
        "Вира 68",
        "Віра 68",
    )


def test_rejects_unconfirmed_complex_rewrite():
    assert not is_conservative_llm_rewrite(
        "Вишня 700, я на свези",
        "Вишня 700, я на зв'язку",
    )


def test_rejects_inserted_words():
    assert not is_conservative_llm_rewrite(
        "Вишня 700",
        "Вишня 700, я на зв'язку",
    )
