import sys

from project_python import select_python


def interpreter(root, folder):
    path = root / folder / "bin" / "python"
    path.parent.mkdir(parents=True)
    path.touch()
    return str(path)


def test_project_environment_is_used(tmp_path):
    preferred = interpreter(tmp_path, ".venv")
    assert select_python(tmp_path) == preferred


def test_old_venv_folder_is_ignored(tmp_path):
    interpreter(tmp_path, "venv")
    assert select_python(tmp_path) == sys.executable


def test_no_local_environment_uses_current_interpreter(tmp_path):
    assert select_python(tmp_path) == sys.executable
