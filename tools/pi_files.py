"""Список файлів для Raspberry Pi (git sparse-checkout) і перевірка його повноти.

Код радіовузла обчислюється з імпортів ``services.radio_node`` в окремому процесі
(включно з імпортами всередині функцій — вони перелічені в ENTRY_MODULES).
Решта потрібних файлів — у EXTRA_FILES.

    .venv/bin/python -m tools.pi_files          # показати, чого бракує / що зайве
    .venv/bin/python -m tools.pi_files --write  # переписати deploy/pi-files.txt
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "deploy" / "pi-files.txt"

# Модулі, які вузол імпортує (частину — всередині функцій, тому статичного імпорту замало).
ENTRY_MODULES = (
    "services.radio_node",
    "services.radio_stream",
    "services.audio_devices",
    "services.ptt_key",
    "services.segment_spool",
    "services.node_control",
    "core.supervisor",
    "core.notification_spectral",
    "core.notification_templates",
    "core.log_security",
    "core.audio",
    "core.segment_timing",
    "core.bounded_queue",
    "core.diagnostics",
    "config",
)

EXTRA_FILES = (
    "requirements-pi.txt",
    "deploy/radio345-node.service",
    "deploy/pi-files.txt",
    "docs/raspberry-pi.md",
    ".gitignore",                 # щоб на Pi приватні .env/.local-* теж не потрапили в git
)

_PROBE = """
import importlib, json, os, sys
root = sys.argv[1]
sys.path.insert(0, root)
for name in json.loads(sys.argv[2]):
    importlib.import_module(name)
files = set()
for mod in list(sys.modules.values()):
    f = getattr(mod, "__file__", None) or ""
    if f.startswith(root + os.sep) and os.sep + ".venv" + os.sep not in f:
        files.add(os.path.relpath(f, root))
print(json.dumps(sorted(files)))
"""


def node_closure():
    """Файли проєкту, які реально імпортує радіовузол (свіжий інтерпретатор)."""
    out = subprocess.run(
        [sys.executable, "-c", _PROBE, str(ROOT), json.dumps(ENTRY_MODULES)],
        capture_output=True, text=True, check=True, cwd=ROOT, timeout=120,
    ).stdout
    return set(json.loads(out))


def expected():
    return node_closure() | set(EXTRA_FILES)


def read_manifest(path=MANIFEST):
    if not path.exists():
        return set()
    return {
        line.strip().lstrip("/") for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def write_manifest(files, path=MANIFEST):
    header = (
        "# Файли для Raspberry Pi: git sparse-checkout set --no-cone --stdin < deploy/pi-files.txt\n"
        "# Не редагуйте вручну: .venv/bin/python -m tools.pi_files --write (повноту перевіряє pytest).\n"
        "# Шляхи з «/» на початку: без нього шаблон no-cone збігається на будь-якій глибині\n"
        "# (.gitignore тягнув ios/.gitignore і claudeSearch/.gitignore).\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + "".join(f"/{f}\n" for f in sorted(files)), encoding="utf-8")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    want = expected()
    if "--write" in argv:
        write_manifest(want)
        print(f"{MANIFEST.relative_to(ROOT)}: {len(want)} файлів")
        return 0
    have = read_manifest()
    missing, extra = sorted(want - have), sorted(have - want)
    for f in missing:
        print(f"бракує: {f}")
    for f in extra:
        print(f"зайве:  {f}")
    if not missing and not extra:
        print(f"Список актуальний: {len(have)} файлів")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
