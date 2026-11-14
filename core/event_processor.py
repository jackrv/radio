from core.analyzer import fmt, is_short_garbage, split_roles


class EventProcessor:
    def __init__(
        self,
        callsigns,
        commands,
        watch,
        short_garbage,
        save_event,
        telegram_service,
        fresh,
        select_recipients,
        db_path,
        log_callsigns_only,
        log,
        rollback=None,
    ):
        self.callsigns = callsigns
        self.commands = commands
        self.watch = watch
        self.short_garbage = short_garbage
        self.save_event = save_event
        self.telegram_service = telegram_service
        self.fresh = fresh
        self.db_path = db_path
        self.log_callsigns_only = log_callsigns_only
        self.log = log
        self.rollback = rollback
        # Особисті режими: функція(wants) -> отримувачі за поточними правами з бази.
        self.select_recipients = select_recipients

    def process(self, text, pcm, raw_text=None):
        cs_found = self.callsigns.find(text)
        cmd_found = self.commands.find(text)
        watch_found = self.watch.find(text)

        to_found, from_found = split_roles(text, self.callsigns)

        cs_str = fmt(cs_found)
        cmd_str = fmt(cmd_found)
        watch_str = fmt(watch_found)
        to_str = fmt(to_found)
        from_str = fmt(from_found)

        if (
            is_short_garbage(text, self.short_garbage)
            and not cs_found
            and not cmd_found
            and not watch_found
        ):
            self.log.info("Відкинуто короткий хибний фрагмент: %s", text)
            return

        event_id = None

        if (
            (cmd_found or (self.log_callsigns_only and cs_found))
            and self.fresh(("db", cs_str, cmd_str))
        ):
            try:
                event_id = self.save_event(
                    cs_str,
                    cmd_str,
                    to_str,
                    from_str,
                    text,
                    self.db_path,
                    **({"raw_text": raw_text} if raw_text is not None else {}),
                )

                self.log.info(
                    "  -> БД #%s | кому: %s | від: %s | команди: %s",
                    event_id,
                    to_str or "-",
                    from_str or "-",
                    cmd_str or "-",
                )
            except Exception:
                self.log.exception(
                    "Помилка збереження події в SQLite"
                )
                if self.rollback is not None:
                    self.rollback(("db", cs_str, cmd_str))

        def wants(mode):
            return (
                mode == "all"
                or (mode == "watch" and bool(watch_found))
                or (mode == "commands" and bool(cmd_found or cs_found))
            )

        recipients = self.select_recipients(wants)

        if recipients and self.fresh(("tg", text)):
            try:
                fix_id = self.telegram_service.register_phrase_for_fix(
                    raw_text if raw_text is not None else text,
                    event_id,
                )

                self.log.info(
                    "  -> в Telegram [отримувачів: %d]: %s",
                    len(recipients),
                    text,
                )

                self.telegram_service.notify(
                    watch_str or "Ефір",
                    to_str,
                    from_str,
                    text,
                    pcm,
                    fix_id=fix_id,
                    recipients=recipients,
                )
            except Exception:
                self.log.exception(
                    "Помилка надсилання сповіщення Telegram"
                )
