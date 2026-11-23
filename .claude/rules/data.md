---
paths:
  - "services/recordings.py"
  - "services/archive_*.py"
  - "services/telegram*.py"
  - "services/event_store.py"
  - "services/receiver_*.py"
  - "services/storage_status.py"
  - "core/recording_trace.py"
  - "core/diagnostics.py"
  - "core/audio.py"
  - "api/**"
---

# Поведінка даних: архів, Telegram, стан прийому

Загальні заборони — в [AGENTS.md](../../AGENTS.md); тут лише правила теми.

- Gap не доводить сон Mac: класифікуйте за станом потоку ДО розриву;
  paused/listening не приховує наступний active-gap.
- Немовлення відкликається через транзакційний outbox із 20 с undo, без HTTP у review
  й backfill старих міток. skipped_revision не дорівнює applied_revision.
- TTL low_rms — 1 година від захоплення, лише без reference/deferred;
  прогноз і prune спільні, WAV видаляється після commit.

Безпека SQLite (WAL, POSIX-lock, backup) — в AGENTS.md, діє завжди.
Де що: [дані](../../docs/data.md), [Telegram](../../docs/telegram.md); ідеї — розділ «Дані й Telegram» у [ideas.md](../../docs/ideas.md).
