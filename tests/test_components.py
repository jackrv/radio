"""Межі компонентів: ефір без БД і бота, MLX лише там, де треба, шари не змішуються."""
import pytest

from tools import components as c

INNER, OUTER = c.graph()
EAGER_INNER, EAGER_OUTER = c.graph(eager_only=True)
CLOSURES = c.process_closures(INNER)
LAYOUT = c.layout(INNER)

# Відомі винятки. Список можна лише скорочувати: новий виняток = рішення, а не дрібниця.
CORE_UPWARD = {
    ("core.callsigns", "services.dictionary_store"),
    ("core.event_processor", "services.telegram_outbox"),
    ("core.log_security", "services.private_files"),
    ("core.audio_debug", "config"),
    ("core.callsigns", "config"),
    ("core.log_security", "config"),
}


def top(module):
    return module.split(".")[0]


def edges():
    return {(m, x) for m, targets in INNER.items() for x in targets}


@pytest.mark.parametrize("module", [m for _, ms in c.PROCESSES.values() for m in ms] + list(c.LEGACY_ENTRIES))
def test_entry_modules_exist(module):
    assert module in INNER


def test_core_goes_up_only_where_already_known():
    found = {(m, x) for m, x in edges() if top(m) == "core" and top(x) in ("services", "api", "tools", "config")}
    assert found - CORE_UPWARD == set(), "core не має залежати від вищих шарів"
    assert CORE_UPWARD - found == set(), "виняток уже прибрано — приберіть його і зі списку"


def test_upper_layers_are_not_imported_from_below():
    order = {"core": 0, "services": 1, "api": 2, "tools": 3}
    bad = {(m, x) for m, x in edges() if order.get(top(m), 9) in (1, 2) and order.get(top(x), -1) > order[top(m)]}
    assert bad == set()


def test_node_has_no_database_bot_or_model():
    node = CLOSURES["node"]
    assert not {m for m in node if "telegram" in m or m.startswith("api")}
    assert "services.sqlite_connection" not in node
    assert not {m for m in node if OUTER[m] & (set(c.MLX) | {"sqlite3", "fastapi", "requests"})}


def test_worker_has_no_bot_api_or_dictionaries():
    worker = CLOSURES["worker"]
    assert not {m for m in worker if "telegram" in m or "dictionary" in m or top(m) in ("api", "tools")}


@pytest.mark.parametrize("name", ["panel", "api"])
def test_panel_and_api_never_reach_mlx(name):
    assert not {m for m in CLOSURES[name] if OUTER[m] & set(c.MLX)}


def test_mlx_is_imported_at_module_level_only_by_recognizer_and_lab():
    eager = {m for m in EAGER_INNER if EAGER_OUTER[m] & set(c.MLX)}
    assert eager <= {"core.recognition", "lab.asr_lab", "lab.whisper_finetune"}
    for name in ("node", "asr", "panel", "api"):
        assert not c.closure(c.PROCESSES[name][1], EAGER_INNER) & eager, name


def test_future_folders_do_not_import_each_other():
    folders = {"server", "worker", "lab"}
    cross = {(m, x) for m, x in edges() if LAYOUT[m] in folders and LAYOUT[x] in folders and LAYOUT[m] != LAYOUT[x]}
    assert cross == set()


def test_shared_does_not_import_server_worker_lab_or_legacy():
    assert {(m, x) for m, x in edges() if LAYOUT[m] == "shared" and LAYOUT[x] != "shared"} <= set()


def test_new_processes_do_not_need_legacy_modules():
    live = {m for ms in CLOSURES.values() for m in ms}
    assert not {m for m in live if LAYOUT[m] == "legacy" and not c.modules()[m].name == "__init__.py"}


def test_relative_and_lazy_imports_are_seen(tmp_path):
    pkg = tmp_path / "core"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "a.py").write_text("from . import b\ndef f():\n    import core.d\n")
    (pkg / "b.py").write_text("from .d import x\n")
    (pkg / "d.py").write_text("")
    inner, _ = c.graph(tmp_path)
    eager, _ = c.graph(tmp_path, eager_only=True)
    assert inner["core.a"] >= {"core.b", "core.d"} and "core.d" not in eager["core.a"]
    assert "core.d" in inner["core.b"]
