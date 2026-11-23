"""Профілі пристроїв: повний набір файлів, без секретів, бот лише в одному профілі."""
import re
import subprocess
import sys

import pytest

from tools import components, profiles

SPARSE = [n for n in profiles.names() if not profiles.load(n).get("full_checkout")]


@pytest.mark.parametrize("name", SPARSE)
def test_manifest_is_current_and_safe(name):
    assert profiles.manifest(name).read_text(encoding="utf-8") == profiles.render(name), \
        "Оновіть: .venv/bin/python -m tools.profiles --write"
    for path in profiles.files(name):
        assert (profiles.ROOT / path).is_file() or path.endswith(".files"), path
        assert not re.search(r"(^|/)\.env($|\.)|\.local-|\.(db|wav|log|sqlite3)$|^tests/|^ios/", path), path


@pytest.mark.parametrize("name", profiles.names())
def test_profile_is_valid(name):
    data = profiles.load(name)
    assert (profiles.ROOT / data["requirements"]).is_file()
    assert not {k for k in {**data["env"], **data["required_env"]} if "TOKEN" in k or "SECRET" in k}


def test_only_one_profile_runs_the_bot():
    assert [n for n in profiles.names() if profiles.load(n)["bot"]] == ["pi"]


@pytest.mark.parametrize("name", SPARSE)
def test_sparse_checkout_imports_every_process(tmp_path, name):
    """Справжній набір файлів: копія лише зі списку має імпортувати всі вхідні модулі процесів."""
    for path in profiles.files(name):
        source, target = profiles.ROOT / path, tmp_path / path
        if source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
    entries = [m for p in profiles.load(name)["processes"] for m in components.PROCESSES[p][1]]
    # MLX заблоковано для Pi: там його немає.
    block = "import sys\n" + ("for n in ('mlx','mlx_whisper','mlx_lm'): sys.modules[n]=None\n" if name == "pi" else "")
    # Ліниві імпорти теж: усі модулі профілю мають завантажуватися.
    skip = set(profiles.load(name).get("exclude_modules", ())) | {"core.correction"}
    inner, _ = components.graph()
    pruned = {m: t - skip for m, t in inner.items()}
    mods = sorted(m for m in components.closure(entries, pruned) - skip)
    code = block + "import importlib\n" + "".join(f"importlib.import_module({m!r})\n" for m in mods)
    done = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"})
    assert done.returncode == 0, done.stderr[-600:]
