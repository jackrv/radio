def build_llm_corrector(
    use_llm,
    llm_repo,
    callsigns,
    commands,
    log,
):
    if not use_llm:
        return None

    try:
        from mlx_lm import generate, load

        log.info("Загружаю LLM для коррекции текста (%s)...", llm_repo)

        llm_model, tokenizer = load(llm_repo)
        known_vocab = ", ".join(
            sorted(set(callsigns.names) | set(commands.names))
        )

        def correct(raw_text: str) -> str:
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
                max_tokens=64,
            )

            cleaned = response.strip().strip('"').strip("'")
            return cleaned if cleaned else raw_text

        log.info("LLM успешно загружена.")
        return correct

    except Exception as e:
        log.error(
            "Не удалось загрузить LLM (%s), работаю без нее.",
            e,
        )
        return None
