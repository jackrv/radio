"""Відтворювана офлайн-обробка записів «Аудіоархіву».

Без Telegram, мікрофона й робочого циклу приймача. Архів і events.db лише
читаються: правила й словники беруться зі знімка бази, зробленого на початку
запуску. Результати й параметри зберігаються окремо в .local-experiments/.

Приклади:
  .venv/bin/python -m lab.asr_lab inventory
  .venv/bin/python -m lab.asr_lab queue          # зафіксована вибірка для офлайн-порівнянь
  .venv/bin/python -m lab.asr_lab run --name base --variant whisper --status ready,empty_text,blacklist
  .venv/bin/python -m lab.asr_lab run --name no-prompt --variant whisper --prompt none
  .venv/bin/python -m lab.asr_lab run --name band --variant whisper --bandpass on
  .venv/bin/python -m lab.asr_lab report .local-experiments/<запуск>
  .venv/bin/python -m lab.asr_lab compare <запуск A> <запуск B>

Змінюйте один чинник за запуск. Повторний прогін Whisper не є еталоном:
якість рахується лише за розшифровками оператора (reference).
"""

import argparse
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import sys
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from shared.sqlite_connection import read_only
from shared.paths import place, root_of

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = place(ROOT, ".local-recordings")
OUTPUT = place(ROOT, ".local-experiments")

# Короткий контекст із підтверджених оператором оборотів (docs/recognition.md).
# Без номерів і без підписів на кшталт «Позивні:» — вони самі потрапляли в галюцинації.
PHRASES_PROMPT = ("Радіообмін. Перевірка зв’язку. На зв’язку. Чую та розбираю добре. "
                  "Прийнято, прийом. Прийняв, прийом. Посилку отримав. "
                  "До роботи приступив. Роботу закінчив. Повторіть, голос не пройшов.")
PROMPTS = ("dictionary", "receiver", "receiver-no-phrases", "receiver-no-dictionary", "receiver-command-codes", "none", "phrases", "combined")
RECEIVER_PROMPTS = ("receiver", "receiver-no-phrases", "receiver-no-dictionary", "receiver-command-codes")

SPLIT_BLOCK_S = 20 * 60
VARIANTS = ("whisper", "rules", "qwen", "pipeline")


# ---------- Дані архіву (лише читання) ----------

def split_of(created):
    """Відкладений набір — кожен третій 20-хвилинний блок ефіру (за хешем).

    Повтори одного виклику й близькі дублікати лежать в одному блоці,
    тож не потрапляють по різні боки поділу.
    """
    block = int(created // SPLIT_BLOCK_S)
    digest = int(hashlib.sha256(f"radio345-split-v1:{block}".encode()).hexdigest(), 16)
    return "holdout" if digest % 3 == 0 else "tune"


def load_records(archive=ARCHIVE, *, statuses=None, labelled_only=False, split="all", limit=None, ids=None):
    with read_only(Path(archive) / "index.sqlite3") as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM recordings ORDER BY created").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["data"] = json.loads(item["data"])
        item["split"] = split_of(item["created"])
        if statuses and item["status"] not in statuses:
            continue
        if labelled_only and item["label"] is None:
            continue
        if split != "all" and item["split"] != split:
            continue
        if ids is not None and item["id"] not in ids:
            continue
        result.append(item)
    return result[:limit] if limit else result


def read_pcm(archive, record_id):
    path = Path(archive) / f"{record_id}.wav"
    with wave.open(str(path), "rb") as audio:
        if audio.getnchannels() != 1 or audio.getsampwidth() != 2:
            raise ValueError("Непідтримуваний WAV")
        return audio.readframes(audio.getnframes()), audio.getframerate()


# ---------- Метрики ----------

# Слова для WER і сімейств зворотів — спільні з архівом (shared/text_metrics.py).
from shared.text_metrics import edit_distance, metric_words as words  # noqa: E402
plain_tokens = words
from shared.lab_results import FAMILIES, NON_SPEECH, ROOM, SPEECH, UNCLEAR, has_family, load_run, role_tokens, summarize  # noqa: E402,F401


def digit_groups(text):
    """Незалежна перевірка номерів: лише цифри, без парсера числівників застосунку."""
    return re.findall(r"\d+", text or "")


def app_number_groups(text):
    from shared.word_learning import number_groups
    return number_groups(text or "")


def score_text(reference, hypothesis):
    ref, hyp = words(reference), words(hypothesis)
    ref_chars, hyp_chars = " ".join(ref), " ".join(hyp)
    return {"word_errors": edit_distance(ref, hyp), "reference_words": len(ref),
            "char_errors": edit_distance(ref_chars, hyp_chars), "reference_chars": len(ref_chars),
            "digits_match": digit_groups(reference) == digit_groups(hypothesis),
            "app_numbers_match": app_number_groups(reference) == app_number_groups(hypothesis),
            "reference_has_digits": bool(digit_groups(reference))}


def bucket(value, edges, unit):
    if value is None:
        return "—"
    for edge in edges:
        if value <= edge:
            return f"≤{edge:g}{unit}"
    return f">{edges[-1]:g}{unit}"


def breakdown(results):
    """WER за тривалістю, гучністю й статусом (лише мовлення з еталоном)."""
    groups = {}
    for r in results:
        if r.get("label") not in SPEECH or r.get("reference") is None:
            continue
        signal = r.get("signal") or {}
        multi = {1: "кілька", 0: "одна"}.get(r.get("multiple_messages"), "не відмічено")
        for key in (("тривалість", bucket(signal.get("duration_s"), (2, 4, 8, 20), " с")),
                    ("гучність", bucket(signal.get("rms_dbfs"), (-38, -34, -30, -24), " dBFS")),
                    ("статус", r.get("status")), ("передач у записі", multi)):
            g = groups.setdefault(key, {"word_errors": 0, "reference_words": 0, "records": 0})
            g["word_errors"] += r["score"]["word_errors"]
            g["reference_words"] += r["score"]["reference_words"]
            g["records"] += 1
    return {f"{k[0]}: {k[1]}": {**v, "wer": v["word_errors"] / v["reference_words"] if v["reference_words"] else None}
            for k, v in sorted(groups.items())}


# ---------- Конвеєр ----------

def bandpass(pcm, sample_rate, low=300.0, high=3400.0, ramp=100.0):
    """Смуга мовлення рації перед Whisper: частотна маска з плавними краями, без зсуву фази.

    Гул кімнати нижче й удари клавіш та шипіння вище смуги послаблюються.
    Обробляє весь запис одразу (офлайн); для живого приймання потрібен потоковий фільтр.
    """
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    if not x.size:
        return pcm
    spectrum = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(len(x), 1.0 / sample_rate)
    rise = np.clip((freqs - (low - ramp)) / ramp, 0.0, 1.0)
    fall = np.clip(((high + ramp) - freqs) / ramp, 0.0, 1.0)
    mask = 0.5 - 0.5 * np.cos(np.pi * np.minimum(rise, fall))
    y = np.fft.irfft(spectrum * mask, len(x))
    return np.clip(np.rint(y), -32768, 32767).astype("<i2").tobytes()


def snapshot_database(target):
    """Знімок events.db для відтворюваності; робоча база не змінюється."""
    import config
    from contextlib import closing
    with closing(read_only(config.DB_PATH)) as source, closing(sqlite3.connect(target)) as copy:
        source.backup(copy)
    os.chmod(target, 0o600)
    # Таблиці правил у копії (стара база могла їх не мати); робоча база не змінюється.
    from shared.word_store import init_word_db
    init_word_db(target)
    return target


def receiver_prompt(db_path, dictionary_prompt, phrases=None, extra=()):
    """Підказка, як у приймачі: фрази списку «Підказка Whisper» + назви словників.

    ``phrases`` — інший перелік фраз (файл --whisper-phrases); ``extra`` — додаткові
    фрази перед назвами (коди доби запису для --daily-codes).
    """
    from shared.text_lists import compose_prompt, load_active
    if phrases is None:
        with read_only(db_path) as db:
            _, values = load_active(db)
        phrases = values.get("whisper_prompt", ())
    return compose_prompt((*phrases, *extra), dictionary_prompt)


def receiver_prompt_variant(db_path, dictionary_prompt, phrases, mode, extra=()):
    """Вимкнути окремо фразовий або словниковий складник підказки приймача."""
    if mode == "receiver-no-phrases":
        phrases = ()
    elif mode == "receiver-no-dictionary":
        dictionary_prompt = ""
    elif mode not in ("receiver","receiver-command-codes"):
        raise ValueError(f"Невідомий режим підказки приймача: {mode}")
    return receiver_prompt(db_path, dictionary_prompt, phrases, extra)


def record_codes(db_path, created, mode="append"):
    """Коди доби запису (доба за межею 20:00) зі знімка бази.

    append — постійні й усі коди доби; latest — лише останній доданий код цієї доби.
    """
    from shared import daily_codes
    day = daily_codes.local_day(created)
    with read_only(db_path) as db:
        try:
            return daily_codes.read_day(db, day)[-1:] if mode == "latest" else daily_codes.read(db, day)
        except sqlite3.Error:
            return []


def dictionaries_from(db_path, archive=None):
    from shared.dictionary_loader import build_prompt, dictionaries_from_lines, read_dictionary_lines
    with read_only(db_path) as db:
        lines = read_dictionary_lines(db)
        try:
            from shared.text_lists import load_active
            from shared.callsigns import configure_aliases
            _, values = load_active(db)
            configure_aliases(values["aliases"], values["callsign_aliases"])
            blacklist, short_garbage = values["blacklist"], values["short_garbage"]
        except sqlite3.Error:
            from shared.default_text_lists import enabled
            blacklist, short_garbage = list(enabled("blacklist")), list(enabled("short_garbage"))
    callsigns, commands, watch = dictionaries_from_lines(lines)
    from shared.dictionary_number_evidence import load_rules
    archive=Path(archive) if archive is not None else place(root_of(db_path), ".local-recordings")
    # Еталони контрольної частини не можуть навчати правило, що їх оцінює.
    allowed_ids=set()
    if (archive/'index.sqlite3').is_file():
        from contextlib import closing
        with closing(read_only(archive/'index.sqlite3')) as index:
            allowed_ids={row[0] for row in index.execute('SELECT id,created FROM recordings') if split_of(row[1])=='tune'}
    callsigns.number_rules=load_rules(archive,callsigns,commands,allowed_ids=allowed_ids)
    return (callsigns, commands, watch), build_prompt(callsigns, commands, watch), blacklist, short_garbage


def prompt_token_count(prompt, language):
    """Кількість токенів підказки токенізатором Whisper (зберігається ≤223 останніх)."""
    if not prompt:
        return 0
    try:
        from mlx_whisper.tokenizer import get_tokenizer
        tokenizer = get_tokenizer(multilingual=True, num_languages=100, language=language or "uk", task="transcribe")
        return len(tokenizer.encode(" " + prompt.strip()))
    except Exception:
        return None


def dictionary_fingerprint_of(db_path):
    from shared.dictionary_loader import dictionary_fingerprint
    try:
        with read_only(db_path) as db:
            return dictionary_fingerprint(db)
    except sqlite3.Error:
        return None


def runtime_versions(model_repo):
    """Версії рушія й ревізія локальних ваг (каталог snapshot HF — це хеш коміту)."""
    from importlib import metadata
    versions = {"python": sys.version.split()[0]}
    for package in ("mlx-whisper", "mlx", "numpy"):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = None
    try:
        from huggingface_hub import try_to_load_from_cache
        path = try_to_load_from_cache(model_repo, "config.json")
        versions["model_revision"] = Path(path).parent.name if isinstance(path, str) else None
    except Exception:
        versions["model_revision"] = None
    return versions


def parse_whisper_options(items):
    """«ключ=значення» для mlx_whisper.transcribe: true/false, числа, інакше рядок."""
    options = {}
    for item in items:
        key, sep, raw = item.partition("=")
        if not sep or not key.strip():
            raise SystemExit(f"--whisper-option: очікується «ключ=значення», отримано «{item}»")
        allowed = {"hallucination_silence_threshold", "word_timestamps", "best_of",
                   "condition_on_previous_text", "no_speech_threshold", "logprob_threshold",
                   "compression_ratio_threshold", "suppress_blank", "suppress_tokens",
                   "length_penalty", "fp16"}
        if key.strip() not in allowed:
            raise SystemExit(f"--whisper-option: параметр «{key.strip()}» не дозволено")
        if key.strip() in options:
            raise SystemExit(f"--whisper-option: параметр «{key.strip()}» повторено")
        lowered = raw.strip().lower()
        if lowered in ("true", "false"):
            value = lowered == "true"
        else:
            try:
                value = float(raw) if any(c in raw for c in ".eE") else int(raw)
            except ValueError:
                value = raw
        key=key.strip()
        if key in {'word_timestamps','condition_on_previous_text','suppress_blank','fp16'}:
            if type(value) is not bool: raise SystemExit(f'{key}: потрібне true/false')
        elif key=='suppress_tokens':
            if not re.fullmatch(r'-?\d+(?:,-?\d+)*',raw.strip()): raise SystemExit('suppress_tokens: потрібні номери токенів через кому')
            value=raw.strip()
        else:
            import math
            if type(value) not in (int,float) or not math.isfinite(value): raise SystemExit(f'{key}: потрібне скінченне число')
            if key=='best_of' and (type(value) is not int or not 1<=value<=10): raise SystemExit('best_of: ціле число 1–10')
            if key=='no_speech_threshold' and not 0<=value<=1: raise SystemExit('no_speech_threshold: число 0–1')
            if key in {'hallucination_silence_threshold','length_penalty'} and value<0: raise SystemExit(f'{key}: число не може бути від’ємним')
            if key=='compression_ratio_threshold' and value<=0: raise SystemExit('compression_ratio_threshold: число має бути додатним')
        options[key] = value
    return options


class Lab:
    def __init__(self, args, run_dir, reuse_snapshot=False):
        import config
        self.args = args
        self.log = logging.getLogger("asr_lab")
        snapshot = run_dir / "events-snapshot.db"
        # Продовження прогону — на тому самому знімку словників і налаштувань, що й початок.
        self.db_path = snapshot if reuse_snapshot and snapshot.is_file() else snapshot_database(snapshot)
        self.dictionaries, dictionary_prompt, self.blacklist, self.short_garbage = dictionaries_from(self.db_path,getattr(args,'archive',None))
        if args.prompt=='receiver-command-codes':
            from shared.dictionary_loader import build_prompt
            dictionary_prompt=build_prompt(*self.dictionaries,include_command_codes=True)
        if getattr(args, "names", None):
            # Інший перелік назв у підказці (напр. лише ті, що звучать в ефірі): рядок — назва.
            names = [l.strip() for l in Path(args.names).read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
            dictionary_prompt = ", ".join(sorted(set(names)))
        phrases = Path(args.prompt_file).read_text(encoding="utf-8").strip() if args.prompt_file else PHRASES_PROMPT
        self.whisper_phrases = None
        if getattr(args, "whisper_phrases", None):
            self.whisper_phrases = [line.strip() for line in Path(args.whisper_phrases).read_text(encoding="utf-8").splitlines()
                                    if line.strip() and not line.startswith("#")]
        if args.prompt in RECEIVER_PROMPTS:
            self.prompt = receiver_prompt_variant(self.db_path, dictionary_prompt, self.whisper_phrases, args.prompt)
        else:
            self.prompt = {"dictionary": dictionary_prompt, "none": "", "phrases": phrases,
                           "combined": f"{phrases} {dictionary_prompt}".strip()}[args.prompt]
        self.dictionary_prompt = "" if args.prompt == "receiver-no-dictionary" else dictionary_prompt
        self.language = None if args.language == "auto" else args.language
        self.model = args.model or config.MODEL_REPO
        self.sample_rate = config.SR
        from shared.recognition import WhisperRecognizer
        self.temperature = tuple(float(t) for t in getattr(args, "temperature", "0,0.2,0.4").split(","))
        self.whisper_options = parse_whisper_options(getattr(args, "whisper_option", None) or [])
        self.recognizer = WhisperRecognizer(self.model, self.language, self.sample_rate, self.prompt, self.log,
                                            temperature=self.temperature, options=self.whisper_options)
        from shared.runtime_settings import _read
        with read_only(self.db_path) as db:
            self.runtime_values, self.runtime_revision = _read(db)
        self.garbage_limits = {
            "min_words": self.runtime_values["GARBAGE_MIN_WORDS"],
            "min_unique_ratio": self.runtime_values["GARBAGE_MIN_UNIQUE_RATIO"],
            "max_words_per_s": self.runtime_values["GARBAGE_MAX_WORDS_PER_S"],
            "extra_words": self.runtime_values["GARBAGE_EXTRA_WORDS"],
        }
        self.llm = None
        if args.variant == "qwen" and getattr(args, "llm", "on") == "off":
            raise SystemExit("--llm off має сенс лише для --variant pipeline")
        if args.variant in ("qwen", "pipeline") and getattr(args, "llm", "on") == "on":
            from shared.correction import build_llm_corrector
            callsigns, commands, _ = self.dictionaries
            self.llm = build_llm_corrector(True, config.LLM_REPO, callsigns, commands, self.log)

    def params(self):
        import config
        return {"variant": self.args.variant, "pipeline_scope": "text-after-whisper-v2",
                "dictionary_number_rules": {"evidence_split": "tune", "count": len(self.dictionaries[0].number_rules)},
                "runtime_settings": self.runtime_values, "runtime_settings_revision": self.runtime_revision,
                "not_replayed": ["notification_spectral", "cooldown", "recipient_modes", "telegram_delivery"],
                "llm": getattr(self.args, "llm", "on") if self.args.variant in ("qwen", "pipeline") else None,
                "llm_repo": config.LLM_REPO if self.llm else None, "temperature": list(self.temperature), "model": self.model, "language": self.language or "auto",
                "prompt": self.args.prompt, "prompt_chars": len(self.prompt), "prompt_text": self.prompt,
                "daily_codes": getattr(self.args, "daily_codes", "off"), "whisper_phrases": self.whisper_phrases,
                "retry": getattr(self.args, "retry", "off"), "names_file": getattr(self.args, "names", None),
                "prompt_tokens": prompt_token_count(self.prompt, self.language),
                "prompt_sha256": hashlib.sha256(self.prompt.encode()).hexdigest(),
                "dictionary_prompt_sha256": hashlib.sha256(self.dictionary_prompt.encode()).hexdigest(),
                "dictionary_fingerprint": dictionary_fingerprint_of(self.db_path),
                "versions": runtime_versions(self.model),
                "normalize": self.args.normalize == "on",
                "min_rms_gate": self.args.min_rms, "llm_repo": config.LLM_REPO if self.llm else None,
                "whisper_options": {"temperature": list(self.temperature), "condition_on_previous_text": False,
                                    "no_speech_threshold": 0.6, "logprob_threshold": -1.0,
                                    "compression_ratio_threshold": 2.4, **self.whisper_options},
                "seed": self.args.seed, "resegment": self.args.resegment, "gate_db": self.args.gate_db,
                "bandpass": [self.args.band_low, self.args.band_high] if self.args.bandpass == "on" else None,
                "split": self.args.split, "statuses": self.args.status, "labelled_only": self.args.labelled_only}

    def apply_rules(self, text):
        from shared.word_rule_engine import apply_verified_word_rules
        revised = apply_verified_word_rules(text, self.db_path, self.dictionaries)
        return revised, revised != text

    def resegment(self, pcm):
        """Повторне різання запису сегментатором VAD із додатковою умовою гучності.

        Кадр вважається мовленням, лише якщо WebRTC VAD так вважає І він гучніший
        за фон запису (10-й перцентиль кадрів) на --gate-db. Так пауза, заповнена
        шумом ефіру, закриває сегмент через SILENCE_END_MS, як справжня тиша.
        """
        import config
        from shared.vad import VADSegmenter
        frame = config.FRAME_LEN * 2
        frames = [pcm[i:i + frame] for i in range(0, len(pcm) - frame + 1, frame)]
        if not frames:
            return [pcm]
        levels = [20 * math.log10(math.sqrt(float(np.mean(np.frombuffer(f, "<i2").astype(np.float64) ** 2)) / 32768 ** 2) + 1e-12)
                  for f in frames]
        # Фон — за кадрами з реальним сигналом: цифрові нулі дали б −240 дБ.
        audible = [v for v in levels if v > -100] or levels
        floor = float(np.percentile(audible, 10))
        level_of = dict(zip(map(id, frames), levels))
        # Невідомі кадри (додана в кінці тиша) — не мовлення.
        gate = lambda f: level_of.get(id(f), -240.0) > floor + self.args.gate_db
        segmenter = VADSegmenter(config.SR, config.FRAME_MS, config.VAD_LEVEL, config.PREROLL_MS,
                                 config.SILENCE_END_MS, config.MIN_SPEECH_MS, config.MAX_SEGMENT_S, speech_gate=gate)
        pieces = []
        silence = b"\0" * frame
        for f in frames + [silence] * (config.SILENCE_END_MS // config.FRAME_MS + 1):
            piece = segmenter.process(f)
            if piece:
                pieces.append(piece)
        return pieces

    def _transcribe_with_retry(self, audio):
        """Однаковий recovery для цілого запису й кожної повторно нарізаної частини."""
        from shared.recognition import is_looped
        whisper = self.recognizer.transcribe(audio)
        text = self.recognizer.extract_text(whisper)
        retried, accepted = False, False
        if getattr(self.args, "retry", "off") == "on" and not text and self.recognizer.prompt and is_looped(whisper):
            retried = True
            again = self.recognizer.transcribe(audio, prompt="")
            recovered = self.recognizer.extract_text(again)
            if recovered:
                whisper, text, accepted = again, recovered, True
        return whisper, text, {"attempted": retried, "accepted": accepted}

    def process(self, pcm):
        import config
        from shared.audio import prepare_audio
        from shared.analyzer import confident_roles, fmt, is_low_confidence_short, looks_like_garbage, split_roles, strip_dictionary_echo
        from shared.pipeline import build_text_pipeline
        from shared.analyzer import correction_is_safe
        from shared.text_lists import blacklist_key
        timing = {}
        started = time.perf_counter()
        audio, rms = prepare_audio(pcm, 0.0, self.args.normalize == "on")
        # Поріг RMS — як у приймачі: за вихідним звуком, до будь-яких фільтрів.
        gated = self.args.min_rms is not None and rms < self.args.min_rms
        result = {"rms": rms, "gated": gated}
        if gated or audio.size == 0:
            result.update(hypothesis="", raw_text="", stage="rms_gate")
            timing["total_s"] = time.perf_counter() - started
            return result, timing
        if self.args.bandpass == "on":
            pcm = bandpass(pcm, self.sample_rate, self.args.band_low, self.args.band_high)
            audio, _ = prepare_audio(pcm, 0.0, self.args.normalize == "on")
        t = time.perf_counter()
        try:
            # Відкат на температуру 0,2/0,4 — випадкова вибірка. Фіксуємо зерно,
            # щоб однакові варіанти давали однаковий текст між запусками.
            import mlx.core as mx
            mx.random.seed(self.args.seed)
        except ImportError:
            pass
        if self.args.resegment == "energy":
            pieces = self.resegment(pcm)
            texts, segments_meta, retries = [], [], []
            for piece in pieces:
                piece_audio, _ = prepare_audio(piece, 0.0, self.args.normalize == "on")
                out, text, retry = self._transcribe_with_retry(piece_audio)
                texts.append(text)
                retries.append(retry)
                segments_meta += [{k: s.get(k) for k in ("avg_logprob", "no_speech_prob", "compression_ratio",
                                                         "temperature", "start", "end", "text")} for s in out.get("segments", [])]
            raw = " ".join(t for t in texts if t).strip()
            result["segments"] = len(pieces)
            result["segment_durations_s"] = [round(len(piece) / (2 * self.sample_rate), 2) for piece in pieces]
            result["segment_texts"] = texts
            result["segment_retries"] = retries
            result["whisper_segments"] = segments_meta
        else:
            whisper, raw, retry = self._transcribe_with_retry(audio)
            if retry["attempted"]:
                result["whisper_retry"] = retry["accepted"]
            result["retry"] = retry
            result["whisper_segments"] = [{k: s.get(k) for k in ("avg_logprob", "no_speech_prob", "compression_ratio",
                                                                 "temperature", "start", "end", "text")}
                                          for s in whisper.get("segments", [])]
        timing["whisper_s"] = time.perf_counter() - t
        result["raw_text"] = raw
        text, stage = raw, "whisper"
        if self.args.variant == "rules":
            text, _ = self.apply_rules(raw)
            stage = "rules"
        elif self.args.variant == "qwen":
            t = time.perf_counter()
            pipeline = build_text_pipeline(self.llm, lambda value: (value, False), self.dictionaries,
                                           correction_is_safe, self.log)
            text = pipeline(raw) if raw else raw
            timing["qwen_s"] = time.perf_counter() - t
            stage = "qwen"
        elif self.args.variant == "pipeline":
            duration = len(pcm) / (2 * self.sample_rate)
            if not raw:
                stage = "empty_text"
            elif looks_like_garbage(raw, duration, **self.garbage_limits):
                text, stage = "", "garbage"
            elif blacklist_key(raw) in {blacklist_key(v) for v in self.blacklist}:
                text, stage = "", "blacklist"
            else:
                # Перелік назв словника з підказки — як у приймачі (server/transcriber.py).
                cleaned, echo = strip_dictionary_echo(raw, self.dictionaries)
                if echo:
                    result["dictionary_echo"] = True
                if echo and not cleaned:
                    text, stage = "", "dictionary_echo"
                elif echo and blacklist_key(cleaned) in {blacklist_key(v) for v in self.blacklist}:
                    text, stage = "", "blacklist"
                elif is_low_confidence_short(cleaned, result["whisper_segments"], self.dictionaries[0],
                                             self.dictionaries[1], max_words=config.LOW_CONFIDENCE_MAX_WORDS,
                                             logprob=config.LOW_CONFIDENCE_LOGPROB):
                    text, stage = "", "low_confidence"
                else:
                    t = time.perf_counter()
                    pipeline = build_text_pipeline(self.llm, self.apply_rules, self.dictionaries, correction_is_safe, self.log)
                    text = pipeline(cleaned)
                    timing["correction_s"] = time.perf_counter() - t
                    from shared.text_analysis import analyze_text
                    analysis = analyze_text(text, *self.dictionaries, self.short_garbage, self.log)
                    text = "" if analysis.rejected else analysis.text
                    stage = analysis.rejected or "pipeline"
        result["hypothesis"] = text
        result["stage"] = stage
        callsigns = self.dictionaries[0]
        to_found, from_found = confident_roles(text, *split_roles(text, callsigns), callsigns)
        result["hypothesis_recipient"], result["hypothesis_sender"] = fmt(to_found), fmt(from_found)
        timing["total_s"] = time.perf_counter() - started
        return result, timing


def roles_from_reference(reference, callsigns):
    """Логіка «кому / від кого» на правильному тексті: окремо від помилок слуху моделі."""
    from shared.analyzer import fmt, split_roles
    to_found, from_found = split_roles(reference, callsigns)
    return fmt(to_found), fmt(from_found)


def keep_awake():
    """Поки йде прогін, Mac не засинає через бездіяльність (без приймача мікрофон закритий).

    18.11.2026 нічний прогін зупинився: при зупиненому приймачі Mac заснув за кілька хвилин.
    caffeinate сам завершується разом із цим процесом.
    """
    import os
    import shutil
    import subprocess
    if shutil.which("caffeinate"):
        try:
            subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass


def run(args):
    import config
    from shared.model_exclusion import lab_model_slot
    parse_whisper_options(getattr(args, "whisper_option", None) or [])
    import math
    try: temperatures=[float(t) for t in getattr(args,'temperature','0,0.2,0.4').split(',')]
    except ValueError: raise SystemExit('Температури Whisper: числа через кому') from None
    if not temperatures or any(not math.isfinite(t) or t<0 for t in temperatures):
        raise SystemExit('Температури Whisper мають бути скінченними невід’ємними числами')
    with lab_model_slot(config.DB_PATH):
        return _run_checked(args)


# Параметри, які мають збігтися, щоб продовження було тим самим прогоном.
RESUME_KEYS = ("variant", "prompt", "model", "language", "temperature", "daily_codes", "whisper_phrases", "retry",
               "names_file", "normalize", "min_rms_gate", "whisper_options", "seed", "resegment", "gate_db",
               "bandpass", "split", "statuses", "labelled_only", "llm", "dictionary_fingerprint")


def resume_dir(value):
    """Тека прогону для --resume: шлях або назва в .local-experiments; лише незавершений прогін."""
    path = Path(value)
    if not path.is_absolute() and not path.exists():
        path = OUTPUT / value
    if not (path / "params.json").is_file() or not (path / "results.jsonl").is_file():
        raise SystemExit(f"Немає незавершеного прогону для продовження: {value}")
    if (path / "metrics.json").is_file():
        raise SystemExit("Прогін уже завершено — продовжувати нічого")
    return path


def read_done(results_path):
    """Готові рядки results.jsonl; обірваний останній рядок (процес зупинили під час запису) відкидається."""
    done, keep = [], 0
    with open(results_path, "rb") as f:
        for line in f:
            if not line.endswith(b"\n"):
                break
            try:
                done.append(json.loads(line))
            except ValueError:
                break
            keep += len(line)
    if keep != results_path.stat().st_size:
        with open(results_path, "r+b") as f:
            f.truncate(keep)
    return done


def _run_checked(args):
    keep_awake()
    if getattr(args, "daily_codes", None) is None:
        # Як у приймачі: код доби в підказці, якщо його ввімкнено в config.py.
        import config
        args.daily_codes = "latest" if config.WHISPER_PROMPT_DAILY_CODE else "off"
    resume = getattr(args, "resume", None)
    statuses = set(args.status.split(",")) if args.status else None
    if resume:
        run_dir = resume_dir(resume)
        saved = json.loads((run_dir / "params.json").read_text())
        ids_file = run_dir / "records.txt"
        if ids_file.is_file():
            ids = ids_file.read_text().split()
        else:
            # Прогін до появи records.txt: ті самі умови вибірки й лише записи, що існували на старті.
            started_at = datetime.strptime(saved["started_utc"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()
            ids = [r["id"] for r in load_records(args.archive, statuses=statuses, labelled_only=args.labelled_only,
                                                  split=args.split, limit=args.limit) if r["created"] <= started_at]
        order = {record_id: i for i, record_id in enumerate(ids)}
        records = sorted(load_records(args.archive, ids=set(ids)), key=lambda r: order[r["id"]])
        lab = Lab(args, run_dir, reuse_snapshot=True)
        current = lab.params()
        changed = [key for key in RESUME_KEYS if key in saved and json.loads(json.dumps(current.get(key))) != saved[key]]
        if changed:
            raise SystemExit("Параметри відрізняються від початку прогону: " + ", ".join(changed))
        results = read_done(run_dir / "results.jsonl")
        done_ids = {item["id"] for item in results}
        params = {**saved, "resumed_utc": [*saved.get("resumed_utc", []), datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")],
                  "missing_on_resume": len(ids) - len(records)}
        mode = "a"
    else:
        started = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_dir = OUTPUT / f"{started}_{re.sub(r'[^A-Za-z0-9_-]+', '-', args.name)}"
        run_dir.mkdir(parents=True, mode=0o700)
        ids = set(Path(args.ids).read_text().split()) if args.ids else None
        records = load_records(args.archive, statuses=statuses, labelled_only=args.labelled_only,
                               split=args.split, limit=args.limit, ids=ids)
        lab = Lab(args, run_dir)
        params = {**lab.params(), "records": len(records), "archive": str(Path(args.archive).resolve()),
                  "started_utc": started, "git_head": os.popen(f"git -C {ROOT} rev-parse HEAD").read().strip()}
        # Перелік записів фіксується: продовження після паузи пройде рівно ті самі.
        (run_dir / "records.txt").write_text("".join(f"{r['id']}\n" for r in records))
        results, done_ids, mode = [], set(), "w"
    OUTPUT.chmod(0o700)
    (run_dir / "params.json").write_text(json.dumps(params, ensure_ascii=False, indent=2))
    with open(run_dir / "results.jsonl", mode, encoding="utf-8") as out:
        for index, record in enumerate(records, 1):
            if record["id"] in done_ids:
                continue
            try:
                pcm, sr = read_pcm(args.archive, record["id"])
                if sr != lab.sample_rate:
                    raise ValueError("Інша частота дискретизації")
                if getattr(args, "daily_codes", "off") != "off" and args.prompt in RECEIVER_PROMPTS:
                    # Як могло б бути в приймачі: коди доби саме цього запису — у підказці.
                    lab.recognizer.prompt = receiver_prompt_variant(
                        lab.db_path, lab.dictionary_prompt, lab.whisper_phrases, args.prompt,
                        record_codes(lab.db_path, record["created"], args.daily_codes))
                output, timing = lab.process(pcm)
            except Exception as exc:  # один зіпсований запис не зупиняє експеримент
                output, timing = {"error": type(exc).__name__}, {}
            try:
                wav_sha = hashlib.sha256((Path(args.archive) / f"{record['id']}.wav").read_bytes()).hexdigest()
            except OSError:
                wav_sha = None
            item = {"id": record["id"], "created": record["created"], "split": record["split"],
                    "wav_sha256": wav_sha, "session": record["data"].get("session"),
                    "status": record["status"], "label": record["label"], "reference": record["reference"],
                    "reference_recipient": record["reference_recipient"], "reference_sender": record["reference_sender"],
                    "multiple_messages": record.get("multiple_messages"),
                    "signal": record["data"].get("signal"), "original_raw_text": record["data"].get("raw_text"),
                    **output, "timing": timing}
            if record["reference"] is not None:
                item["score"] = score_text(record["reference"], output.get("hypothesis", ""))
                item["reference_recipient_parsed"], item["reference_sender_parsed"] = roles_from_reference(
                    record["reference"], lab.dictionaries[0])
            out.write(json.dumps(item, ensure_ascii=False) + "\n")
            out.flush()
            results.append(item)
            print(f"\r{index}/{len(records)}", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)
    write_report(run_dir, results)
    print(run_dir)


metrics_for = summarize


def write_report(run_dir, results):
    report = {"summary": metrics_for(results), "breakdown": breakdown(results),
              "by_split": {s: metrics_for([r for r in results if r.get("split") == s]) for s in ("tune", "holdout")}}
    (Path(run_dir) / "metrics.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def compare(a_dir, b_dir, show=10):
    params_a, a = load_run(a_dir)
    params_b, b = load_run(b_dir)
    common = {r["id"] for r in a} & {r["id"] for r in b}
    a = [r for r in a if r["id"] in common]
    b = [r for r in b if r["id"] in common]
    sa, sb = metrics_for(a), metrics_for(b)
    print(f"Спільних записів: {len(common)}\n")
    for key in sorted(set(sa) | set(sb)):
        print(f"  {key:40s} {sa.get(key)!s:>22} → {sb.get(key)!s}")
    by_id = {r["id"]: r for r in b}
    changes = []
    for r in a:
        other = by_id[r["id"]]
        if "score" in r and "score" in other and r["score"]["word_errors"] != other["score"]["word_errors"]:
            changes.append((other["score"]["word_errors"] - r["score"]["word_errors"], r, other))
    print(f"\nПокращень: {sum(d < 0 for d, _, _ in changes)}, погіршень: {sum(d > 0 for d, _, _ in changes)}")
    for delta, r, other in sorted(changes, key=lambda c: c[0])[:show] + sorted(changes, key=lambda c: -c[0])[:show]:
        print(f"  {delta:+d} слів | {r['id'][:8]} | еталон: {r['reference']!r}\n        A: {r.get('hypothesis')!r}\n        B: {other.get('hypothesis')!r}")


def inventory(args):
    records = load_records(args.archive)
    from collections import Counter
    statuses = Counter(r["status"] for r in records)
    labels = Counter(r["label"] for r in records)
    print(f"Записів: {len(records)} | статуси: {dict(statuses)}")
    print(f"Мітки: {dict(labels)} | еталонів: {sum(r['reference'] is not None for r in records)}")
    print(f"Ролі: кому {sum(r['reference_recipient'] is not None for r in records)}, "
          f"від {sum(r['reference_sender'] is not None for r in records)}")
    print(f"Поділ: {dict(Counter(r['split'] for r in records))} "
          f"(розмічених: {dict(Counter(r['split'] for r in records if r['label'] is not None))})")


QUEUE_GROUPS = (
    ("A", "Усе, що бачив Whisper (ready, empty_text, blacklist)"),
    ("B", "Відсічені за RMS поруч із порогом (−36…−34 dBFS)"),
    ("C", "Відсічені за RMS, нижче −36 dBFS"),
    ("D", "Відкинуті VAD, мовлення 300–490 мс"),
)


def build_queue(records, *, seed=345, sizes=(None, 15, 10, 20)):
    """Стратифікована вибірка нерозмічених записів; детермінована за зерном."""
    import random
    rng = random.Random(seed)
    pending = [r for r in records if r["label"] is None]
    rms = lambda r: (r["data"].get("signal") or {}).get("rms_dbfs") or -120
    selectors = (
        lambda r: r["status"] in ("ready", "empty_text", "blacklist", "low_confidence"),
        lambda r: r["status"] == "low_rms" and -36 <= rms(r) < -34,
        lambda r: r["status"] == "low_rms" and rms(r) < -36,
        lambda r: r["status"] == "vad_short" and (r["data"].get("speech_ms") or 0) >= 300,
    )
    groups = []
    for (key, title), select, size in zip(QUEUE_GROUPS, selectors, sizes):
        ids = sorted(r["id"] for r in pending if select(r))
        if size is not None:
            rng.shuffle(ids)
            ids = sorted(ids[:size])
        groups.append({"key": key, "title": title, "ids": ids})
    return {"version": 1, "created": time.time(), "seed": seed, "groups": groups}


def write_queue(args):
    import config
    queue = build_queue(load_records(args.archive), seed=args.seed)
    path = Path(args.output or config.REVIEW_QUEUE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(queue, ensure_ascii=False, indent=1))
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    for group in queue["groups"]:
        print(f"{group['key']}: {len(group['ids'])} — {group['title']}")
    print(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--archive", default=str(ARCHIVE))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("inventory")
    qp = sub.add_parser("queue", help="зафіксувати вибірку записів для офлайн-порівнянь")
    qp.add_argument("--seed", type=int, default=345)
    qp.add_argument("--output", help="типово config.REVIEW_QUEUE_PATH")
    r = sub.add_parser("run")
    r.add_argument("--name", required=True)
    r.add_argument("--resume", help="продовжити незавершений прогін (тека або її назва) з місця зупинки")
    r.add_argument("--variant", choices=VARIANTS, default="whisper")
    r.add_argument("--status", help="статуси через кому (типово — усі)")
    r.add_argument("--split", choices=("all", "tune", "holdout"), default="all")
    r.add_argument("--labelled-only", action="store_true")
    r.add_argument("--limit", type=int)
    r.add_argument("--ids", help="файл з ідентифікаторами записів")
    r.add_argument("--language", default="uk", help="uk, ru або auto")
    r.add_argument("--prompt", choices=PROMPTS, default="dictionary",
                   help="dictionary — лише назви словників; receiver — як у приймачі (список «Підказка Whisper» "
                        "+ назви); phrases — лише звороти; combined — звороти + словник")
    r.add_argument("--prompt-file", help="власний текст зворотів для phrases/combined (без номерів)")
    r.add_argument("--whisper-phrases", help="файл фраз «Підказки Whisper» замість списку з бази (для --prompt receiver)")
    r.add_argument("--daily-codes", choices=("off", "append", "latest"), default=None,
                   help="append: додати в підказку постійні й коди доби кожного запису; latest — лише останній "
                        "код доби запису (лише --prompt receiver)")
    r.add_argument("--retry", choices=("off", "on"), default="off",
                   help="on: другий шанс без підказки, коли Whisper зациклився й тексту немає (як у приймачі)")
    r.add_argument("--names", help="файл назв для підказки замість усіх назв словників (рядок — назва)")
    r.add_argument("--normalize", choices=("on", "off"), default="on")
    r.add_argument("--temperature", default="0,0.2,0.4",
                   help="температури Whisper через кому; «0» — без повторного розпізнавання")
    r.add_argument("--llm", choices=("on", "off"), default="on",
                   help="off: pipeline без Qwen (решта обробки як у приймачі)")
    r.add_argument("--min-rms", type=float, default=None, help="поріг RMS (типово — без відсікання)")
    r.add_argument("--model", help="інша модель Whisper (типово — з config.py)")
    r.add_argument("--seed", type=int, default=345, help="зерно для випадкового відкату температури")
    r.add_argument("--resegment", choices=("none", "energy"), default="none",
                   help="energy — повторно різати запис VAD із умовою гучності над фоном")
    r.add_argument("--gate-db", type=float, default=12.0, help="поріг над фоном для --resegment energy, дБ")
    r.add_argument("--bandpass", choices=("off", "on"), default="off", help="смуга мовлення перед Whisper")
    r.add_argument("--band-low", type=float, default=300.0, help="нижня межа смуги, Гц")
    r.add_argument("--band-high", type=float, default=3400.0, help="верхня межа смуги, Гц")
    r.add_argument("--whisper-option", action="append", metavar="КЛЮЧ=ЗНАЧЕННЯ",
                   help="додатковий параметр mlx_whisper.transcribe, напр. hallucination_silence_threshold=2 "
                        "(можна кілька разів)")
    rep = sub.add_parser("report")
    rep.add_argument("run_dir")
    c = sub.add_parser("compare")
    c.add_argument("a")
    c.add_argument("b")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    if args.command == "inventory":
        inventory(args)
    elif args.command == "queue":
        write_queue(args)
    elif args.command == "run":
        run(args)
    elif args.command == "report":
        _, results = load_run(args.run_dir)
        print(json.dumps(write_report(args.run_dir, results), ensure_ascii=False, indent=2))
    else:
        compare(args.a, args.b)


if __name__ == "__main__":
    main()
