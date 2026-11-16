from core.analyzer import (
    canonical_text, fmt, fmt_spoken, is_bare_callsign, is_short_garbage, split_roles,
)


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

        from services.recordings import note
        note(predicted_recipient=to_str, predicted_sender=from_str, roles_parsed=True)

        if (
            is_short_garbage(text, self.short_garbage)
            and not cs_found
            and not cmd_found
            and not watch_found
        ):
            self.log.info("Відкинуто короткий хибний фрагмент: %s", text)
            return

        if not cmd_found and not watch_found and is_bare_callsign(text, cs_found, self.callsigns):
            note(bare_callsign=True)
            self.log.info("Відкинуто: лише позивний без номера, без команди й тексту: %s", text)
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

                # Зв’язок запису архіву з подією: правка в Mini App стане його еталоном.
                note(fix_id=f"db:{event_id}")
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
                if fix_id:
                    note(fix_id=fix_id)

                # У сповіщенні впізнані зі словником слова пишуться як у словнику,
                # а знайдені команди — окремим рядком шапки. У базі текст незмінний.
                # Збій оформлення не має зупинити сповіщення: тоді йде почутий текст.
                try:
                    shown = canonical_text(text, (self.commands, self.callsigns, self.watch))
                    commands_str = fmt_spoken(cmd_found)
                except Exception:
                    self.log.exception("Не вдалося оформити текст сповіщення; надсилаю почутий")
                    shown, commands_str = text, cmd_str
                notification = self.telegram_service.notify(
                    watch_str or "Ефір",
                    to_str,
                    from_str,
                    shown,
                    pcm,
                    fix_id=fix_id,
                    recipients=recipients,
                    commands_str=commands_str,
                )
                if notification:
                    self.log.info(
                        "  -> черга Telegram [notification=%s; отримувачів: %d]: %s",
                        notification, len(recipients), shown,
                    )
            except Exception:
                self.log.exception(
                    "Помилка надсилання сповіщення Telegram"
                )
