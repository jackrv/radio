from unittest.mock import Mock

from core.pipeline import build_text_pipeline


def make_pipeline(
    correct_with_llm=None,
    apply_learned_corrections=None,
    correction_is_safe=None,
):
    log = Mock()
    dictionaries = ("callsigns", "commands", "watch")

    if apply_learned_corrections is None:
        apply_learned_corrections = Mock(
            side_effect=lambda text: (text, False)
        )

    if correction_is_safe is None:
        correction_is_safe = Mock(return_value=True)

    process_text = build_text_pipeline(
        correct_with_llm=correct_with_llm,
        apply_learned_corrections=apply_learned_corrections,
        dictionaries=dictionaries,
        correction_is_safe=correction_is_safe,
        log=log,
        llm_guard=lambda old, new: True,
    )

    return process_text, log, dictionaries


def test_learned_correction_is_passed_to_llm():
    apply_learned = Mock(side_effect=[
        ("Исправленный текст", True),
        ("Исправленный текст плюс", False),
    ])
    llm = Mock(return_value="Исправленный текст плюс")

    process_text, log, _ = make_pipeline(
        correct_with_llm=llm,
        apply_learned_corrections=apply_learned,
    )

    result = process_text("Исходный текст")

    assert result == "Исправленный текст плюс"
    llm.assert_called_once_with("Исправленный текст")
    assert log.info.call_count == 2


def test_safe_llm_correction_is_accepted():
    apply_learned = Mock(side_effect=[
        ("Исходный текст", False),
        ("Исправленный LLM текст", False),
    ])
    llm = Mock(return_value="Результат LLM")
    is_safe = Mock(return_value=True)

    process_text, log, dictionaries = make_pipeline(
        correct_with_llm=llm,
        apply_learned_corrections=apply_learned,
        correction_is_safe=is_safe,
    )

    result = process_text("Исходный текст")

    assert result == "Исправленный LLM текст"
    llm.assert_called_once_with("Исходный текст")
    # Проверяем исходное предложение Qwen,
    # до дополнительных словарных замен.
    is_safe.assert_called_once_with(
        "Исходный текст",
        "Результат LLM",
        dictionaries,
    )
    log.info.assert_called_once()


def test_post_llm_learned_correction_is_applied():
    apply_learned = Mock(side_effect=[
        ("Исходный текст", False),
        ("Исправленный обучением текст", True),
    ])
    llm = Mock(return_value="Результат LLM")
    is_safe = Mock(return_value=True)

    process_text, log, _ = make_pipeline(
        correct_with_llm=llm,
        apply_learned_corrections=apply_learned,
        correction_is_safe=is_safe,
    )

    result = process_text("Исходный текст")

    assert result == "Исправленный обучением текст"
    assert log.info.call_count == 2


def test_unsafe_llm_correction_returns_original_text():
    apply_learned = Mock(side_effect=[
        ("Исходный текст", False),
        ("Ухудшенный текст", False),
    ])
    llm = Mock(return_value="Результат LLM")
    is_safe = Mock(return_value=False)

    process_text, log, _ = make_pipeline(
        correct_with_llm=llm,
        apply_learned_corrections=apply_learned,
        correction_is_safe=is_safe,
    )

    result = process_text("Исходный текст")

    assert result == "Исходный текст"
    log.warning.assert_called_once()


def test_without_llm_returns_learned_result():
    apply_learned = Mock(return_value=("Результат обучения", False))

    process_text, _, _ = make_pipeline(
        correct_with_llm=None,
        apply_learned_corrections=apply_learned,
    )

    result = process_text("Исходный текст")

    assert result == "Результат обучения"
    apply_learned.assert_called_once_with("Исходный текст")
