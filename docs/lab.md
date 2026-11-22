# 🧪 Лабораторія

[← Вікі](README.md) · [Подробиці: черги, SQLite, рецепти, історія результатів](lab-details.md)

Перевірка змін на **ваших розмічених записах** до того, як вони потраплять у приймач.

> ⚠️ Модель Whisper — лише одна на Mac (16 ГБ). Ручний `tools.asr_lab` чи
> `tools.whisper_finetune train`: спершу `start_radio345.py --receiver-stop`,
> після роботи/помилки — `--receiver-start`. Панельна черга керує цим сама
> (режим «лише запис», запобіжник до 4 год). MacBook Air без вентилятора
> перегрівається від годин Whisper поспіль (18.11, «Thermal Emergency Sleep»):
> довгі серії — з перервами, на живленні.

## Повтор тексту — без моделей

`tools/text_replay` повторює текстову обробку на збережених виходах Whisper.
Приймач **не зупиняти**, база й архів лише читаються. Порожні виходи входять
у знаменник (пропуски мовлення). Аудіофільтри й упевненість Whisper тут не
відтворюються — це не заміна ASR-прогону.

```bash
.venv/bin/python -m tools.text_replay                          # чинні шаблони проти стану до 17.11
.venv/bin/python -m tools.text_replay --patterns FILE --each   # внесок кожного шаблону фраз
```

Те саме в панелі: «Перевірити на моїх еталонах» у фільтрах тексту та
«Аналіз → Вплив правил» (фонова черга, стан у `.local-text-checks/`).

## Повний прогін — з Whisper

`tools/asr_lab` відтворює обробку записів архіву поза приймачем. Результати —
`.local-experiments/<час>_<назва>/` (`params.json`, `results.jsonl`, `metrics.json`);
перегляд — **Аналіз → Експерименти**, порівняння з `q-base` лише на спільних ID
з однаковими еталонами й ролями.

```bash
.venv/bin/python -m tools.asr_lab run --name base  --variant whisper --ids ids.txt --prompt receiver
.venv/bin/python -m tools.asr_lab run --name codes --variant whisper --ids ids.txt --prompt receiver --daily-codes latest
.venv/bin/python -m tools.asr_lab compare .local-experiments/<A> .local-experiments/<B>
```

| Параметр | Що змінює |
| --- | --- |
| `--variant` | `whisper`, `rules`, `qwen` або `pipeline` (текстовий конвеєр; без фільтра сигналів, cooldown і Telegram) |
| `--prompt` | `receiver` (як у приймачі), `receiver-no-phrases`, `receiver-no-dictionary`, `receiver-command-codes`, `dictionary`, `phrases`, `combined`, `none` |
| `--whisper-phrases FILE` | інші фрази «Підказки Whisper» |
| `--daily-codes` | `append` — коди доби запису; `latest` — лише останній код доби |
| `--resegment energy` | розбити склеєні передачі за паузами (є `segment_retries`) |
| `--bandpass`, `--temperature`, `--model`, `--min-rms` | обробка звуку й декодування |
| `--whisper-option` | інші опції; `beam_size`, `patience` і дублікати заборонені |

Пауза й продовження: тест черги можна поставити на паузу (⏸) — приймач одразу повертає
розпізнавання — і продовжити з того самого запису (▶ зараз або ⏲ за вікнами часу). Наприкінці
вікна тест сам стає на паузу й продовжується в наступному. Вручну:
`tools.asr_lab run … --resume <тека>` — ті самі записи (`records.txt`), той самий знімок бази;
інші параметри рецепта відхиляються.

Рецепти черги в панелі (22.11.2026): `base`, `dictionary-blocks`, `word-silence`, `resegment`; вирішені досліди
(без підказки/коду доби/фраз/словників, без другого шансу, bandpass, turbo) прибрано, параметри CLI лишилися.

Ще не запущені рецепти (живий ефір не змінено):
`dictionary-blocks` (`--prompt receiver-command-codes`, коди команд у підказці) і
`word-silence` (`word_timestamps` + `hallucination_silence_threshold=2`; дивитися
й пропуски, й час ASR). Без моделі: `.venv/bin/python -m tools.audio_text_audit`;
прямий енергетичний відсів [не пройшов контроль](reports/2026-11-21-audio-text-consistency.md).
Старі `resegment`-прогони — не чисте порівняння, потрібен новий парний.

## Як порівнювати чесно

1. Ті самі записи (`--ids`), один чинник за раз; фіксуйте параметри й коміт.
2. Не лише WER: числа, команди, позивні, пропущене мовлення, вигадки на шумі,
   затримка. Менший WER через порожню відповідь — не покращення.
3. Розкид Whisper між однаковими прогонами — 1–2 п.п. можуть бути шумом.
4. Остаточний висновок — на новій сесії ефіру; конфігурацію міняємо лише за
   виміряного виграшу. Правила номерів будуються за `tune`; перевірка — `holdout`.

Підсумки дослідів 16–18.11 (large-v3, підказка, другий шанс, коди доби тощо) —
у [таблиці результатів](lab-details.md#результати).

## Дообучення Whisper

Набір: **Аналіз → Якість по днях → Набір для дообучення** →
`.local-training/<час>/` (`audio/*.wav`, `metadata.jsonl`; останні ≈10 % днів — `test`).
Пілот tiny: `python -m tools.whisper_finetune prepare --help` / `train --help`.
Підготовка може йти під час ефіру, навчання — лише з вільним model slot.
Ваги не вмикаються автоматично, resume оптимізатора немає.
[Результат пілота](reports/2026-11-21-tiny-finetuning-pilot.md): мовлення краще,
вигадки на немовленні гірші — не ввімкнено.

## Аналіз із командного рядка

Ті самі розбори, що «Аналіз» у панелі, без зупинки приймача:

```bash
.venv/bin/python -m tools.analysis summary
.venv/bin/python -m tools.analysis errors --days 7
.venv/bin/python -m tools.analysis records callsigns -n 10
```

`--json` — сирі дані; також `conditions`, `callsigns`, `checks`, `runs`,
`compare`, `queue`. Повний перелік — `.venv/bin/python -m tools.analysis -h`.
