from core.llm_safety import is_conservative_llm_rewrite


def build_text_pipeline(
    correct_with_llm,
    apply_learned_corrections,
    dictionaries,
    correction_is_safe,
    log,
    llm_guard=is_conservative_llm_rewrite,
):
    """Создаёт обработчик текста после распознавания Whisper."""

    def process_text(raw_text: str) -> str:
        text, learned = apply_learned_corrections(raw_text)

        if learned:
            log.info("Исправлено по обучению (до LLM): %s", text)

        if correct_with_llm:
            llm_text = correct_with_llm(text)

            # Проверяем предложение Qwen ДО словарных замен.
            if (
                llm_guard(text, llm_text)
                and correction_is_safe(text, llm_text, dictionaries)
            ):
                llm_text, learned_post = apply_learned_corrections(llm_text)
                text = llm_text
                log.info("Распознано (LLM): %s", text)

                if learned_post:
                    log.info("Исправлено по обучению (после LLM): %s", text)
            else:
                log.warning(
                    "LLM отклонена; сохраняю результат словесных правил: %s",
                    text,
                )

        return text

    return process_text
