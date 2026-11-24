# 🧪 Лабораторія

[← Вікі](README.md) · [Результати дослідів і правила прогонів](lab-details.md)

Перевірка змін на **ваших розмічених записах** до того, як вони потраплять у приймач.

> ⚠️ Модель Whisper — лише одна на Mac (16 ГБ). Ручний `lab.asr_lab` чи `lab.whisper_finetune train`:
> спершу `./r345 pause-model`, після роботи/помилки — `./r345 resume-model`. Панельна черга керує цим сама
> (строк паузи до 4 год). Лабораторія працює лише на Mac. MacBook Air перегрівається від годин Whisper поспіль: довгі серії — з перервами.

## Повтор тексту — без моделей

`lab/text_replay` повторює текстову обробку на збережених виходах Whisper. Приймач **не зупиняти**, база й архів лише
читаються. Порожні виходи входять у знаменник. Аудіофільтри й упевненість не відтворюються — це не заміна ASR-прогону.

```bash
.venv/bin/python -m lab.text_replay                          # чинні шаблони проти стану до 17.11
.venv/bin/python -m lab.text_replay --patterns FILE --each   # внесок кожного шаблону фраз
```

Те саме в панелі: «Перевірити на моїх еталонах» у фільтрах тексту та
«Аналіз → Вплив правил» (фонова черга, стан у `.local-data/text-checks/`).

## Повний прогін — з Whisper

`lab/asr_lab` відтворює обробку записів архіву поза приймачем. Результати —
`.local-data/experiments/<час>_<назва>/` (`params.json`, `results.jsonl`, `metrics.json`);
перегляд — **Аналіз → Експерименти**, порівняння з `q-base` лише на спільних ID
з однаковими еталонами й ролями.

```bash
.venv/bin/python -m lab.asr_lab run --name base  --variant whisper --ids ids.txt --prompt receiver
.venv/bin/python -m lab.asr_lab run --name codes --variant whisper --ids ids.txt --prompt receiver --daily-codes latest
.venv/bin/python -m lab.asr_lab compare .local-data/experiments/<A> .local-data/experiments/<B>
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

Пауза (⏸) одразу повертає приймачу розпізнавання; продовження — з того самого запису (▶ зараз або ⏲ за вікнами). Наприкінці
вікна тест сам стає на паузу до наступного. Вручну: `lab.asr_lab run … --resume <тека>` — ті самі записи (`records.txt`)
й знімок бази; інші параметри рецепта відхиляються.

Рецепти черги (22.11.2026): `base`, `dictionary-blocks` (`--prompt receiver-command-codes`), `word-silence` (`word_timestamps` +
`hallucination_silence_threshold=2`), `resegment`; три останні [відхилено 23.11](reports/2026-11-23-lab-recipes-and-deferred.md).
Вирішені досліди (без підказки/коду доби/фраз/словників, без другого шансу, bandpass, turbo) прибрано, параметри CLI лишилися.
Без моделі: `.venv/bin/python -m tools.audio_text_audit`; енергетичний відсів
[не пройшов контроль](reports/2026-11-21-audio-text-consistency.md).

## Як порівнювати чесно

1. Ті самі записи (`--ids`), один чинник за раз; фіксуйте параметри й коміт.
2. Не лише WER: числа, команди, позивні, пропущене мовлення, вигадки на шумі,
   затримка. Менший WER через порожню відповідь — не покращення.
3. Розкид Whisper між однаковими прогонами — 1–2 п.п. можуть бути шумом.
4. Остаточний висновок — на новій сесії ефіру; конфігурацію міняємо лише за
   виміряного виграшу. Правила номерів будуються за `tune`; перевірка — `holdout`.

Досліди 16–18.11 — у [таблиці результатів](lab-details.md#результати).

## Дообучення Whisper

Набір: **Аналіз → Якість по днях → Набір для дообучення** →
`.local-data/training/<час>/` (`audio/*.wav`, `metadata.jsonl`; останні ≈10 % днів — `test`).
`python -m lab.whisper_finetune prepare --help` / `train --help`: підготовка може йти під час ефіру, навчання — лише з вільним
model slot. Ваги не вмикаються автоматично, resume оптимізатора немає. [Пілот tiny](reports/2026-11-21-tiny-finetuning-pilot.md)
не ввімкнено: мовлення краще, вигадки на немовленні гірші.

## Аналіз із командного рядка

Як «Аналіз» у панелі, без зупинки приймача:

```bash
.venv/bin/python -m lab.analysis summary
.venv/bin/python -m lab.analysis errors --days 7
.venv/bin/python -m lab.analysis records callsigns -n 10
```

`--json` — сирі дані; також `conditions`, `callsigns`, `checks`, `runs`,
`compare`, `queue`. Повний перелік — `.venv/bin/python -m lab.analysis -h`.
