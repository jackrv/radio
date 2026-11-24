---
paths:
  - "shared/analyzer.py"
  - "shared/callsigns.py"
  - "shared/command_completion.py"
  - "shared/correction.py"
  - "shared/dictionary_blocks.py"
  - "shared/notification_spectral.py"
  - "shared/phrase_patterns.py"
  - "shared/pipeline.py"
  - "shared/recognition.py"
  - "shared/template_text.py"
  - "shared/text_analysis.py"
  - "shared/text_metrics.py"
  - "shared/transcript_format.py"
  - "shared/vad.py"
  - "shared/whisper_result.py"
  - "server/transcriber.py"
  - "server/event_processor.py"
  - "config.py"
  - "radio.py"
  - "shared/suggestions.py"
  - "shared/word_*.py"
  - "shared/archive_learning.py"
  - "shared/noise_candidates.py"
  - "lab/**"
  - "shared/lab_*.py"
  - "shared/error_analysis.py"
  - "shared/experiments.py"
  - "s*/dictionary_*.py"
  - "shared/alias_candidates.py"
  - "shared/training_set.py"
  - "shared/text_*.py"
  - "shared/daily_codes.py"
  - "server/transcriber_factory.py"
  - "shared/model_exclusion.py"
---

# Розпізнавання й експерименти

Основні правила й заборони розпізнавання — в [AGENTS.md](../../AGENTS.md); тут доповнення.

- Конфлікти навчання показують альтернативу й джерела; «Дозволити» їх не усуває.
  Правильний еталон не змінюють заради універсального правила.

Де що: [розпізнавання](../../docs/recognition.md), [лабораторія](../../docs/lab.md),
[навчання](../../docs/learning.md), [словники](../../docs/dictionaries.md);
ідеї — розділ «Розпізнавання» у [ideas.md](../../docs/ideas.md).
