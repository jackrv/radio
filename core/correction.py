from core.diagnostics import runtime


def build_llm_corrector(
    use_llm,
    llm_repo,
    callsigns,
    commands,
    log,
):
    if not use_llm:
        runtime.gauge("models.qwen", "disabled")
        return None

    try:
        from mlx_lm import generate, load

        log.info("Завантажую LLM для корекції тексту (%s)...", llm_repo)

        runtime.gauge("models.qwen", "loading")
        with runtime.timed("asr.qwen_load"):
            llm_model, tokenizer = load(llm_repo)
        known_vocab = ", ".join(
            sorted(set(callsigns.names) | set(commands.names))
        )

        def correct(raw_text: str) -> str:
            try:
                prompt = (
                    "Ти помічник оператора радіоефіру. "
                    "Виправляй очевидні помилки автоматичного "
                    "розпізнавання мовлення.\n"
                    f"Відомі позивні та команди: {known_vocab}.\n"
                    "Зберігай мову оригінального повідомлення. "
                    "Не перекладай українські слова російською. "
                    "Не замінюй українські літери російськими.\n"
                    "Використовуй правильне написання позивних "
                    "із наданого словника, якщо впевнений у відповідності.\n"
                    "Не вигадуй і не змінюй цифри, номери та позивні. "
                    "Не додавай інформації, якої немає в оригіналі.\n"
                    "Якщо виправлення ненадійне, залиш текст без змін.\n"
                    "Поверни лише виправлений текст, "
                    "без пояснень і лапок.\n\n"
                    f"Текст: {raw_text}\n"
                    "Виправлений текст:"
                )

                messages = [{"role": "user", "content": prompt}]
                text_prompt = tokenizer.apply_chat_template(
                    messages,
                    tokenize=False,
                    add_generation_prompt=True,
                )

                response = generate(
                    llm_model,
                    tokenizer,
                    prompt=text_prompt,
                    verbose=False,
                    max_tokens=min(512, max(64, len(raw_text) * 2)),
                )

                cleaned = response.strip().strip('"').strip("'")
                return cleaned if cleaned else raw_text
            except Exception:
                runtime.count("llm.generation_error")
                log.exception("Помилка LLM-корекції; використовую початковий текст")
                return raw_text

        log.info("LLM успішно завантажено.")
        runtime.gauge("models.qwen", "ready")
        return correct

    except Exception as e:
        runtime.gauge("models.qwen", "failed")
        log.error(
            "Не вдалося завантажити LLM (%s), працюю без неї.",
            e,
        )
        return None
