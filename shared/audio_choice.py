"""Вибір звукових пристроїв цієї машини: файл .local-audio.json, не база — база переїжджає між Mac і Pi.

Зберігається лише вибране; `.env` важливіший. Діє після перезапуску ефіру.
З Mac для Pi: ``python -m shared.audio_choice read`` або ``write <json>`` (у каталозі проєкту).
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from shared.paths import place

ROOT = Path(__file__).resolve().parent.parent
NAME = ".local-audio.json"
KEYS = ("RADIO_INPUT_DEVICE", "RADIO_OUTPUT_DEVICE")
MAX_NAME = 200


class Conflict(Exception):
    pass


def clean_name(value):
    if not isinstance(value, str):
        raise ValueError("Назва пристрою має бути текстом")
    value = value.strip()
    if len(value) > MAX_NAME or any(ord(ch) < 32 for ch in value):
        raise ValueError("Некоректна назва пристрою")
    return value


def _stored(root):
    try:
        data = json.loads(place(root, NAME).read_text(encoding="utf-8"))
        values = {k: clean_name(v) for k, v in data["values"].items() if k in KEYS}
        revision = data["revision"]
        if type(revision) is not int or revision < 0:
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}, 0
    return values, revision


def read(root=ROOT):
    import config
    values, revision = _stored(root)
    env = set(os.environ)
    try:
        from dotenv import dotenv_values
        env |= set(dotenv_values(Path(root) / ".env"))
    except ImportError:
        pass
    defaults = {key: (getattr(config, key, "") or "").strip() for key in KEYS}
    return {"values": {**defaults, **values}, "revision": revision, "env_override": [k for k in KEYS if k in env]}


def write(root, values, revision):
    values = {key: clean_name(values.get(key, "")) for key in KEYS}
    if _stored(root)[1] != revision:
        raise Conflict("Налаштування пристроїв змінилися. Оновіть сторінку.")
    fd, temp = tempfile.mkstemp(dir=root)   # 0600
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump({"values": values, "revision": revision + 1}, out, ensure_ascii=False)
    os.replace(temp, place(root, NAME))
    return read(root)


def apply(root, target):
    """Старт ефіру: вибір → атрибути config (`.env` усе одно важливіший)."""
    for key, value in _stored(root)[0].items():
        setattr(target, key, value)


def listing(root=ROOT):
    """Вибір і список. Список — в окремому процесі: свіжий після USB і без PortAudio в панелі."""
    result = {"settings": read(root), "devices": None, "error": None}
    try:
        run = subprocess.run([sys.executable, "-m", "shared.audio_devices", "--json"], cwd=ROOT,
                             capture_output=True, text=True, timeout=15)
        if run.returncode:
            raise ValueError
        result["devices"] = json.loads(run.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        result["error"] = "Не вдалося отримати список звукових пристроїв"
    return result


def main(argv):
    try:
        if argv[:1] == ["read"]:
            data = listing()
        elif argv[:1] == ["write"]:
            payload = json.loads(argv[1])
            data = {"settings": write(ROOT, payload["values"], payload["revision"])}
        else:
            return 2
    except Conflict as exc:
        data = {"conflict": str(exc)}
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        data = {"invalid": str(exc)}
    print(json.dumps(data, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
