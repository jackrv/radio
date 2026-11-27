"""Вибір звукових пристроїв за частиною назви (USB-аудіокарта рації).

Номери пристроїв змінюються після перепідключення, тому в налаштуваннях —
частина назви без урахування регістру (`RADIO_INPUT_DEVICE`, `RADIO_OUTPUT_DEVICE`;
`.env` має пріоритет над config.py). Порожньо — системний пристрій за замовчуванням.
Якщо заданий пристрій не знайдено, мовчки на інший не перемикаємось: приймач
повторює спробу (кожні 5 с), передача відмовляє — щоб звук не пішов «не туди».

Список пристроїв: `.venv/bin/python -m shared.audio_devices` (або `--json`).
Вибір у панелі («Пристрої» → «Звук») — окремо для кожної машини (shared/audio_choice.py), діє після перезапуску ефіру.
"""

import os
import re
import sys
from pathlib import Path


NULL_OUTPUT = "null"   # RADIO_OUTPUT_DEVICE: передача у порожнечу, без звукового виходу


class DeviceNotFound(RuntimeError):
    pass


def configured(name, config, env=None):
    env = os.environ if env is None else env
    value = env.get(name)
    if value is None:
        value = getattr(config, name, "")
    return (value or "").strip()


def find_device(name, kind, devices=None):
    """Індекс пристрою або None для системного; ``kind``: input | output."""
    if not name:
        return None
    if devices is None:
        import sounddevice as sd
        devices = sd.query_devices()
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    wanted = name.casefold()
    matches = [i for i, d in enumerate(devices) if d.get(key, 0) > 0 and wanted in d["name"].casefold()]
    if not matches:
        raise DeviceNotFound(f"Звуковий пристрій «{name}» ({'вхід' if kind == 'input' else 'вихід'}) не знайдено")
    # Точний збіг назви важливіший за перший частковий.
    exact = [i for i in matches if devices[i]["name"].casefold() == wanted]
    return (exact or matches)[0]


_current_input = {"name": None}


def current_input():
    """Назва відкритого вхідного пристрою (для профілю запису) або None."""
    return _current_input["name"]


def input_stream_factory(name, log=None):
    """Фабрика для ApplicationSupervisor: пристрій шукається при кожному (пере)відкритті."""
    import sounddevice as sd

    state = {"reported": None}

    def open_stream(**kwargs):
        try:
            device = find_device(name, "input")
        except DeviceNotFound as e:
            if log and state["reported"] != "missing":
                log.error("%s; повторюю спробу", e, extra={"console": True})
            state["reported"] = "missing"
            raise
        try:
            _current_input["name"] = sd.query_devices(device, "input")["name"]
        except Exception:
            _current_input["name"] = None
        if log and state["reported"] != device:
            label = _current_input["name"] if device is not None else "системний"
            log.info("Вхід звуку: %s", label, extra={"console": True})
            state["reported"] = device
        if rechunk_on_this_platform() and kwargs.get("blocksize"):
            # PortAudio/ALSA з фіксованим блоком на Pi віддавав ~40 % кадрів (input overflow),
            # зі змінним — повний потік; VAD потрібні рівні кадри, тож ріжемо самі.
            kwargs["callback"] = rechunking_callback(
                kwargs["callback"], kwargs["blocksize"], kwargs["samplerate"],
                bytes_per_frame=2 * kwargs.get("channels", 1))
            kwargs["blocksize"] = 0
        return sd.RawInputStream(device=device, **kwargs)

    return open_stream


def rechunk_on_this_platform(platform=None):
    return (platform or sys.platform).startswith("linux")


class _ChunkTime:
    __slots__ = ("currentTime", "inputBufferAdcTime")

    def __init__(self, current, adc):
        self.currentTime = current
        self.inputBufferAdcTime = adc


def rechunking_callback(callback, frames_per_chunk, sample_rate, bytes_per_frame=2):
    """Callback PortAudio зі змінним блоком → виклики ``callback`` рівними кадрами.

    Залишок чекає наступного виклику; час АЦП кожного кадру зсунуто на його
    позицію відносно початку поточного буфера (для залишку — від'ємну).
    Стан потоку (overflow тощо) передається з першим кадром після нього.
    """
    chunk_bytes = frames_per_chunk * bytes_per_frame
    state = {"tail": b"", "status": None}

    def on_audio(indata, frame_count, time_info, status):
        if status:
            state["status"] = status
        carried = len(state["tail"]) // bytes_per_frame
        data = state["tail"] + bytes(indata)
        try:
            current = float(time_info.currentTime)
            adc = float(time_info.inputBufferAdcTime)
        except (AttributeError, TypeError, ValueError):
            current = adc = 0.0
        pos = 0
        while len(data) - pos >= chunk_bytes:
            offset = pos // bytes_per_frame - carried
            chunk_time = _ChunkTime(current, adc + offset / sample_rate if adc > 0 else 0.0)
            callback(data[pos:pos + chunk_bytes], frames_per_chunk, chunk_time, state["status"])
            state["status"] = None
            pos += chunk_bytes
        state["tail"] = data[pos:]

    return on_audio


def describe():
    import sounddevice as sd
    lines = []
    for i, d in enumerate(sd.query_devices()):
        kinds = []
        if d["max_input_channels"]:
            kinds.append(f"вхід ×{d['max_input_channels']}")
        if d["max_output_channels"]:
            kinds.append(f"вихід ×{d['max_output_channels']}")
        lines.append(f"{i:>3}  {d['name']}  ({', '.join(kinds)}; {int(d['default_samplerate'])} Гц)")
    default_in, default_out = sd.default.device
    lines.append(f"Системні: вхід {default_in}, вихід {default_out}")
    return "\n".join(lines)


def list_devices():
    """Входи й виходи для вибору в панелі (назва — те, що зберігається в налаштуваннях)."""
    import sounddevice as sd
    devices = sd.query_devices()
    default_in, default_out = sd.default.device
    result = {"inputs": [], "outputs": []}
    for i, d in enumerate(devices):
        for kind, key, default in (("inputs", "max_input_channels", default_in), ("outputs", "max_output_channels", default_out)):
            if d[key]:
                result[kind].append({"name": d["name"], "channels": int(d[key]), "default": i == default})
    if sys.platform.startswith("linux"):
        add_busy(result)
    return result


def add_busy(result, proc="/proc/asound"):
    """PortAudio/ALSA не бачить зайняту карту (її тримає сам ефір): дописуємо такі з /proc під тією ж назвою."""
    root = Path(proc)
    try:
        cards = dict(re.findall(r"^\s*(\d+) \[[^\]]*\]: \S+ - (.+?)\s*$", (root / "cards").read_text(), re.M))
        lines = (root / "pcm").read_text().splitlines()
    except OSError:
        return
    for line in lines:
        head, *parts = [p.strip() for p in line.split(":")]
        match = re.fullmatch(r"(\d+)-(\d+)", head)
        if not match or not parts:
            continue
        card, device = int(match[1]), int(match[2])
        for kind, word, letter in (("inputs", "capture", "c"), ("outputs", "playback", "p")):
            if not any(p.startswith(word) for p in parts):
                continue
            try:
                subs = [f.read_text().strip() for f in root.glob(f"card{card}/pcm{device}{letter}/sub*/status")]
            except OSError:
                continue
            name = f"{cards.get(str(card), f'card {card}')}: {parts[0]} (hw:{card},{device})"
            if any(s and s != "closed" for s in subs) and all(f"(hw:{card},{device})" not in x["name"] for x in result[kind]):
                result[kind].append({"name": name, "channels": None, "default": False, "busy": True})


if __name__ == "__main__":
    import sys
    if "--json" in sys.argv:
        import json
        print(json.dumps(list_devices(), ensure_ascii=False))
    else:
        print(describe())
