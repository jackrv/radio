# 🩺 Діагностика

[← Вікі](README.md)

```bash
.venv/bin/python -m tools.diagnose                            # коротко
.venv/bin/python -m tools.diagnose --verify-backup --network  # ще й копія бази та мережа
.venv/bin/python -m tools.diagnose --json                     # увесь звіт, без текстів, аудіо й токенів
```

Лише читання: служби не зупиняє, повідомлень не надсилає. Код виходу 1 — помилка бази, копії чи мережі.

## Метрики

`.local-data/diagnostics.json` — останній знімок (оновлюється близько 30 с); кожна сесія окремо — `.local-data/diagnostics/`
(14 днів / 100 сесій / 50 МіБ). Перегляд — **Система → Діагностика**. Після аварії лишається `session_status=running`: це не означає,
що процес живий. Середні й лічильники — за сесію; **p95 — за останні 128 сегментів**.

| Група | Що показує |
| --- | --- |
| `telegram.*` | спроби, HTTP-коди, підтвердження, черга |
| `asr.*`, `models.*`, `workers.*` | тривалість етапів, причини відсіву (`asr.outcome.*`), стан потоків |
| `audio.*`, `vad.*` | втрати кадрів і сегментів, зависання мікрофона |
| `receiver.*` | розриви циклу від 60 с; це не доказ сну Mac |
| `asr.health` | `ok` / `stuck` (> 3 хв) / `failing` (5 помилок поспіль) |
| `backup.*`, `recordings.*` | автокопії, запис архіву |

`confirmed` — Telegram відповів `ok=true`, а не «прочитано». При тайм-ауті голосове могло дійти: повтор може дати дубль.

## Затримка сегмента

`asr_latency.ready` — агрегати, `recent_segments` — останні 128 (лише час).

```text
total_latency_s = vad_release_s + queue_wait_s + processing_to_text_s
vad_release_s   = speech_tail_s + capture_to_enqueue_s
```

`whisper_s` — Whisper, `correction_s` — правила після нього, `rtf` — обробка / довжина аудіо, `event_s` — подія й постановка сповіщення
(не доставка). Порожня черга в знімку не доводить відсутності затримок; p95 етапів не додаються.

## Перевірки для розробника

- `node tools/check_panel_ui.cjs` — панель у справжньому Chrome на синтетичній базі.
- `.venv/bin/python -m tools.check_sqlite_concurrency [--seconds 120 --interval .02]` — чотири процеси SQLite на тимчасових базах.
- Якість Whisper лічильники не показують: для неї [еталони](archive.md) й [лабораторія](lab.md).

Журнали містять назви етапів замість розпізнаного тексту; токен маскується.
