from core.llm_safety import is_conservative_llm_rewrite
from core.diagnostics import runtime


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
            log.info("Виправлено за навчанням (до LLM): %s", text)

        if correct_with_llm:
            with runtime.timed("asr.qwen"):
                llm_text = correct_with_llm(text)

            # Проверяем предложение Qwen ДО словарных замен.
            conservative = llm_guard(text, llm_text)
            dictionary_safe = conservative and correction_is_safe(text, llm_text, dictionaries)
            if dictionary_safe:
                runtime.count("llm.accepted")
                if llm_text == text:
                    runtime.count("llm.unchanged")
                llm_text, learned_post = apply_learned_corrections(llm_text)
                text = llm_text
                log.info("Розпізнано (LLM): %s", text)

                if learned_post:
                    log.info("Виправлено за навчанням (після LLM): %s", text)
            else:
                runtime.count("llm.rejected_dictionary" if conservative else "llm.rejected_rewrite")
                log.warning(
                    "LLM відхилено; зберігаю результат словесних правил: %s",
                    text,
                )

        return text

    return process_text
