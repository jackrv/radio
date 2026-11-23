"""Профілі пристроїв: процеси, залежності, параметри й список файлів для git sparse-checkout.

    .venv/bin/python -m tools.profiles            # перевірити списки
    .venv/bin/python -m tools.profiles --write    # переписати deploy/profiles/*.files

Список обчислюється з карти процесів (tools/components.py), уручну його не редагують.
"""
import subprocess
import sys
import tomllib
from pathlib import Path

from tools import components

ROOT = components.ROOT
FOLDER = ROOT / "deploy" / "profiles"
KEYS = {"description", "processes", "requirements", "bot", "extra", "extra_modules", "exclude_modules",
        "full_checkout", "manual", "env", "required_env"}


def names():
    return sorted(p.stem for p in FOLDER.glob("*.toml"))


def load(name):
    with open(FOLDER / f"{name}.toml", "rb") as f:
        data = tomllib.load(f)
    unknown = set(data) - KEYS
    if unknown:
        raise ValueError(f"{name}: невідомі ключі {sorted(unknown)}")
    bad = set(data["processes"]) - set(components.PROCESSES)
    if bad:
        raise ValueError(f"{name}: невідомі процеси {sorted(bad)}")
    return data


def files(name):
    """Відсортовані шляхи або None — повний checkout."""
    data = load(name)
    if data.get("full_checkout"):
        return None
    inner, _ = components.graph()
    known = components.modules()
    skip = set(data.get("exclude_modules", ()))
    pruned = {m: ts - skip for m, ts in inner.items()}
    entries = [m for p in data["processes"] for m in components.PROCESSES[p][1]] + data.get("extra_modules", [])
    found = components.closure(entries, pruned) - skip
    result = {str(known[m].relative_to(ROOT)) for m in found}
    # Пакети: кожен каталог модуля потребує свого __init__.py, якщо він є.
    for path in list(result):
        parent = Path(path).parent
        while parent != Path("."):
            if (ROOT / parent / "__init__.py").is_file():
                result.add(str(parent / "__init__.py"))
            parent = parent.parent
    # Файли даних поруч із модулями (core/default_text_lists.txt): за імпортами їх не видно.
    folders = {str(Path(p).parent) for p in result}
    listed = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "--", "core", "services", "api"],
                            cwd=ROOT, capture_output=True, text=True, check=True).stdout.split("\n")
    result.update(p for p in listed if p and not p.endswith(".py") and str(Path(p).parent) in folders)
    result.add(data["requirements"])
    for item in data.get("extra", ()):
        target = ROOT / item
        if target.is_dir():
            tracked = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "--", item], cwd=ROOT,
                                     capture_output=True, text=True, check=True).stdout.split("\n")
            result.update(t for t in tracked if t)
        else:
            result.add(item)
    result.add(f"deploy/profiles/{name}.files")
    return sorted(result)


def manifest(name):
    return FOLDER / f"{name}.files"


def render(name):
    head = (f"# Профіль {name}: git sparse-checkout set --no-cone --stdin < deploy/profiles/{name}.files\n"
            "# Не редагуйте вручну: .venv/bin/python -m tools.profiles --write (перевіряє pytest).\n")
    return head + "".join(f"/{f}\n" for f in files(name))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    stale = 0
    for name in names():
        wanted = files(name)
        if wanted is None:
            print(f"{name}: увесь репозиторій")
            continue
        text = render(name)
        if "--write" in argv:
            manifest(name).write_text(text, encoding="utf-8")
        current = manifest(name).read_text(encoding="utf-8") if manifest(name).is_file() else ""
        stale += current != text
        print(f"{name}: {len(wanted)} файлів{'' if current == text else ' — список застарів, потрібен --write'}")
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
