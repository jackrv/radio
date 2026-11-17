from core.analyzer import fmt, fmt_spoken
from core.diagnostics import runtime
from core.text_analysis import analyze_text


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
        original = text
        analysis = analyze_text(text, self.callsigns, self.commands, self.watch, self.short_garbage, self.log)
        text = analysis.text
        if raw_text is None and text != original:
            raw_text = original
        cs_found, cmd_found, watch_found = analysis.callsigns, analysis.commands, analysis.watch
        to_found, from_found = analysis.recipient, analysis.sender

        cs_str = fmt(cs_found)
        cmd_str = fmt(cmd_found)
        watch_str = fmt(watch_found)
        to_str = fmt(to_found)
        from_str = fmt(from_found)

        from core.recording_trace import note
        note(predicted_recipient=to_str, predicted_sender=from_str, roles_parsed=True)
        if text != original:
            note(final_text=text)

        if analysis.rejected:
            note(final_text="", rejected_text=text)
            runtime.asr_status(analysis.rejected)
            runtime.count(f"asr.{analysis.rejected}_filtered")
            if analysis.rejected == "bare_callsign":
                note(bare_callsign=True)
                self.log.info("Відкинуто: лише позивний без номера, без команди й тексту: %s", text)
            else:
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

                # Знайдені команди — окремим рядком шапки, так, як їх вимовляють.
                # Збій оформлення не має зупинити сповіщення.
                shown = text
                try:
                    commands_str = fmt_spoken(cmd_found, analysis.partial_commands)
                except Exception:
                    self.log.exception("Не вдалося оформити рядок команд; надсилаю без нього")
                    commands_str = cmd_str
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
