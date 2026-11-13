def build_text_pipeline(
    correct_with_llm,
    apply_learned_corrections,
    dictionaries,
    correction_is_safe,
    log,
):
    """Создаёт обработчик текста после распознавания Whisper."""

    def process_text(raw_text: str) -> str:
        text, learned = apply_learned_corrections(raw_text)

        if learned:
            log.info("Исправлено по обучению (до LLM): %s", text)

        elif correct_with_llm:
            llm_text = correct_with_llm(raw_text)
            llm_text, learned_post = apply_learned_corrections(llm_text)

            if correction_is_safe(raw_text, llm_text, dictionaries):
                text = llm_text
                log.info("Распознано (LLM): %s", text)

                if learned_post:
                    log.info("Исправлено по обучению (после LLM): %s", text)
            else:
                text = raw_text
                log.warning(
                    "LLM ухудшила распознавание, оставляю исходный текст: %s",
                    raw_text,
                )

        return text

    return process_text
