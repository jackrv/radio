"""Локальні приватні WAV-зразки. Жодних моделей чи налаштувань якості."""

import fcntl
import io
import os
from pathlib import Path
import re
import secrets
import subprocess
import wave
from contextlib import contextmanager

import numpy as np

from core.notification_templates import MAX_TEMPLATES, MAX_RECORDING_S, _prepare_template

MAX_UPLOAD_BYTES = 200_000
SAMPLE_ID = re.compile(r"[0-9a-f]{32}\.wav\Z")


@contextmanager
def _locked(directory):
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink():
        raise ValueError("Каталог зразків не може бути посиланням")
    fd = os.open(directory / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield directory
    finally:
        os.close(fd)


def validate_wav(content, sample_rate=16000):
    if not 0 < len(content) <= MAX_UPLOAD_BYTES:
        raise ValueError("Зразок завеликий або порожній")
    try:
        with wave.open(io.BytesIO(content), "rb") as audio:
            if (audio.getnchannels()!=1 or audio.getsampwidth()!=2 or audio.getframerate()!=sample_rate
                    or not 0<audio.getnframes()<=MAX_RECORDING_S*sample_rate):
                raise ValueError("Потрібен моно WAV PCM16, 16000 Гц, до 5 секунд")
            count = audio.getnframes()
            raw = audio.readframes(count)
        if len(raw)!=count*2:
            raise ValueError("Неповний WAV")
        samples = np.frombuffer(raw,dtype="<i2").astype(np.float64)/32768
        _prepare_template(samples, sample_rate)
        # Rebuild PCM-only WAV: discard arbitrary file metadata.
        output = io.BytesIO()
        with wave.open(output,"wb") as audio:
            audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(sample_rate)
            audio.writeframes(raw)
        return output.getvalue(), count/sample_rate
    except (wave.Error, EOFError) as exc:
        raise ValueError("Непридатний WAV-зразок") from exc


def add_sample(directory, content, *, confirmed_isolated=False):
    if confirmed_isolated is not True:
        raise ValueError("Підтвердь, що запис містить лише сповіщення, без мовлення")
    content, duration = validate_wav(content)
    with _locked(directory) as root:
        if len(list(root.glob("*.wav"))) >= MAX_TEMPLATES:
            raise ValueError(f"Дозволено не більше {MAX_TEMPLATES} зразків")
        sample_id = secrets.token_hex(16)+".wav"
        fd = os.open(root/sample_id, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd,"wb") as output:
                output.write(content); output.flush(); os.fsync(output.fileno())
        except BaseException:
            (root/sample_id).unlink(missing_ok=True)
            raise
    return {"id":sample_id,"duration_s":duration,"requires_restart":True}


def sample_audio(directory, sample_id):
    """WAV зразка для прослуховування в панелі; лише файли з каталогу зразків, без посилань."""
    root = Path(directory)
    if not isinstance(sample_id, str) or "/" in sample_id or not sample_id.endswith(".wav") or sample_id.startswith("."):
        raise LookupError("Зразок не знайдено")
    path = root / sample_id
    if root.is_symlink() or path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_UPLOAD_BYTES:
        raise LookupError("Зразок не знайдено")
    return path.read_bytes()


def list_samples(directory):
    root = Path(directory)
    if root.is_symlink():
        raise ValueError("Каталог зразків не може бути посиланням")
    if not root.exists():
        return {"items":[],"requires_restart":True}
    items = []
    for path in sorted(root.glob("*.wav"))[:MAX_TEMPLATES]:
        if path.is_symlink():
            continue
        try:
            _, duration = validate_wav(path.read_bytes()) if path.stat().st_size<=MAX_UPLOAD_BYTES else (None,None)
        except (ValueError,OSError):
            duration=None
        items.append({"id":path.name,"duration_s":duration,"managed":bool(SAMPLE_ID.fullmatch(path.name)),
                      "created":path.stat().st_mtime})
    return {"items":items,"requires_restart":True}


def delete_sample(directory, sample_id):
    if not SAMPLE_ID.fullmatch(sample_id):
        raise ValueError("Неприпустимий ідентифікатор зразка")
    with _locked(directory) as root:
        (root/sample_id).unlink(missing_ok=True)
    return {"deleted":True,"requires_restart":True}


def ogg_to_wav(content, max_seconds=MAX_RECORDING_S):
    # Resource limits and fixed format; never construct a command from user input.
    raw = subprocess.run(["ffmpeg","-loglevel","error","-i","pipe:0","-t",f"{max_seconds + 0.1:.1f}",
        "-ac","1","-ar","16000","-f","s16le","pipe:1"], input=content,
        capture_output=True, check=True, timeout=15).stdout
    if not 0 < len(raw) <= round(16000*max_seconds)*2:
        raise ValueError(f"Зразок має тривати не більше {max_seconds:g} секунд")
    output = io.BytesIO()
    with wave.open(output,"wb") as audio:
        audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(16000); audio.writeframes(raw)
    return output.getvalue()
