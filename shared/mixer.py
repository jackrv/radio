"""Підсилення входу ефіру: регулятори запису мікшера ALSA (amixer), лише Linux.

Карта — з назви вибраного входу «… (hw:N,M)» / «(plughw:N,M)»; для системного входу —
перша карта з регуляторами запису. Панель змінює значення одразу; вибране зберігає
shared/audio_choice.py, а ефір застосовує його при старті (після перезавантаження Pi
мікшер інакше повертається до збереженого системою).
"""
import re
import subprocess
import sys

CARD = re.compile(r"\((?:plug)?hw:(\d+),\d+\)")
CONTROL = re.compile(r"^Simple mixer control '(.+)',(\d+)$")


def available(platform=None):
    return (platform or sys.platform).startswith("linux")


def _amixer(*args, run=subprocess.run):
    done = run(["amixer", *args], capture_output=True, text=True, timeout=10)
    if done.returncode:
        raise OSError((done.stderr or "amixer").strip()[:200])
    return done.stdout


def parse(text):
    """Вивід ``amixer -c N`` → регулятори, що стосуються запису."""
    controls, current = [], None
    for line in text.splitlines():
        head = CONTROL.match(line)
        if head:
            current = {"name": head[1], "index": int(head[2]), "caps": "", "min": None, "max": None,
                       "value": None, "db": None, "switch": None, "seen": False}
            controls.append(current)
            continue
        if current is None:
            continue
        line = line.strip()
        if line.startswith("Capabilities:"):
            current["caps"] = line
        elif line.startswith("Limits:"):
            limits = re.search(r"Capture (-?\d+) - (-?\d+)", line) or (
                None if "Playback" in line else re.search(r"(-?\d+) - (-?\d+)", line))
            if limits:
                current["min"], current["max"] = int(limits[1]), int(limits[2])
        elif current["seen"] is False and not line.startswith(("Capture channels", "Playback channels")):
            channel = re.match(r"^[^:]+:.*?\bCapture\b(.*)$", line)
            if not channel:
                continue
            current["seen"] = True
            part = channel[1]
            value = re.match(r"\s*(-?\d+)", part)
            if value and "cvolume" in current["caps"]:
                current["value"] = int(value[1])
            db = re.search(r"\[(-?[\d.]+)dB\]", part)
            if db:
                current["db"] = float(db[1])
            switch = re.search(r"\[(on|off)\]", part)
            if switch and "cswitch" in current["caps"]:
                current["switch"] = switch[1] == "on"
    result = []
    for c in controls:
        volume = "cvolume" in c["caps"] and c["min"] is not None and c["value"] is not None
        switch = "cswitch" in c["caps"] and c["switch"] is not None
        if not (volume or switch):
            continue
        item = {"name": c["name"], "index": c["index"], "switch": c["switch"] if switch else None}
        if volume:
            item.update(min=c["min"], max=c["max"], value=c["value"], db=c["db"])
        result.append(item)
    return result


def card_of(device, run=subprocess.run):
    """Номер карти ALSA для назви входу; None — регуляторів немає."""
    match = CARD.search(device or "")
    if match:
        return int(match[1])
    try:
        cards = re.findall(r"^card (\d+):", _amixer_cards(run), re.M)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for card in dict.fromkeys(int(c) for c in cards):
        try:
            if parse(_amixer("-c", str(card), run=run)):
                return card
        except (OSError, subprocess.TimeoutExpired):
            continue
    return None


def _amixer_cards(run):
    done = run(["arecord", "-l"], capture_output=True, text=True, timeout=10)
    return done.stdout


def read(device, run=subprocess.run):
    """{"card", "controls", "error"} для вибраного входу."""
    if not available():
        return {"card": None, "controls": [], "error": None}
    card = card_of(device, run)
    if card is None:
        return {"card": None, "controls": [], "error": None}
    try:
        return {"card": card, "controls": parse(_amixer("-c", str(card), run=run)), "error": None}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"card": card, "controls": [], "error": f"Мікшер недоступний: {exc}"}


def set_control(card, name, index=0, value=None, switch=None, run=subprocess.run):
    args = ["-c", str(card), "-q", "sset", f"{name},{int(index)}"]
    if value is not None:
        args.append(str(int(value)))
    if switch is not None:
        args.append("cap" if switch else "nocap")
    if value is None and switch is None:
        return
    _amixer(*args, "capture", run=run)


def apply(device, saved, log=None, run=subprocess.run):
    """Старт ефіру: збережені значення → мікшер, якщо вхід той самий."""
    if not available() or not saved or saved.get("device") != device:
        return
    card = card_of(device, run)
    if card is None:
        return
    for item in saved.get("controls", []):
        try:
            set_control(card, item["name"], item.get("index", 0), item.get("value"), item.get("switch"), run=run)
        except (OSError, subprocess.TimeoutExpired, KeyError, TypeError, ValueError) as exc:
            if log:
                log.warning("Підсилення «%s» не застосовано: %s", item.get("name"), exc)
