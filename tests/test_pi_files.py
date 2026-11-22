"""deploy/pi-files.txt має містити все, що імпортує радіовузол (див. tools/pi_files.py)."""

from pathlib import Path

from tools import pi_files


def test_manifest_covers_radio_node_imports():
    have = pi_files.read_manifest()
    missing = sorted(pi_files.expected() - have)
    assert not missing, (
        "У deploy/pi-files.txt бракує файлів для Raspberry Pi: "
        + ", ".join(missing) + ". Оновіть: .venv/bin/python -m tools.pi_files --write"
    )


def test_manifest_paths_are_anchored_to_repo_root():
    lines = [line for line in pi_files.MANIFEST.read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.startswith("#")]
    assert lines and all(line.startswith("/") for line in lines)


def test_manifest_files_exist_and_are_not_private():
    root = Path(pi_files.ROOT)
    for name in pi_files.read_manifest():
        assert (root / name).is_file(), name
        assert not name.startswith((".env", ".local-")), name
        assert not name.endswith((".db", ".wav", ".log")), name


def test_node_does_not_pull_heavy_receiver_parts():
    closure = pi_files.node_closure()
    for heavy in ("core/recognition.py", "core/transcriber.py", "services/telegram.py",
                  "services/event_store.py", "api/local_dashboard.py", "radio.py"):
        assert heavy not in closure, heavy
