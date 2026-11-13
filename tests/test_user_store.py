import sqlite3

import pytest

from services.user_store import (
    bootstrap_admin,
    create_user,
    init_users_db,
    require_permission,
)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "users.db"
    init_users_db(path)
    return path


def test_initialization_creates_table(db_path):
    with sqlite3.connect(db_path) as db:
        names = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    assert "telegram_users" in names


def test_initial_admin_bootstrap_is_idempotent(db_path):
    assert bootstrap_admin(db_path, 1001) is True
    assert bootstrap_admin(db_path, 1001) is False
    assert require_permission(
        db_path, 1001, "users.manage"
    ) == "admin"


def test_cannot_bootstrap_second_admin(db_path):
    bootstrap_admin(db_path, 1001)
    with pytest.raises(RuntimeError):
        bootstrap_admin(db_path, 1002)


def test_cannot_bootstrap_over_existing_users(db_path):
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO telegram_users (user_id,role) "
            "VALUES (42,'viewer')"
        )
    with pytest.raises(RuntimeError):
        bootstrap_admin(db_path, 1001)


def test_viewer_permissions(db_path):
    bootstrap_admin(db_path, 1001)
    create_user(db_path, 1001, 2002)

    assert require_permission(
        db_path, 2002, "status.read"
    ) == "viewer"

    assert require_permission(
        db_path, 2002, "dictionaries.read"
    ) == "viewer"

    with pytest.raises(PermissionError):
        require_permission(
            db_path, 2002, "corrections.write"
        )


def test_editor_permissions(db_path):
    bootstrap_admin(db_path, 1001)
    create_user(db_path, 1001, 2002, role="editor")

    assert require_permission(
        db_path, 2002, "corrections.write"
    ) == "editor"

    assert require_permission(
        db_path, 2002, "dictionaries.write"
    ) == "editor"

    with pytest.raises(PermissionError):
        require_permission(
            db_path, 2002, "audio.control"
        )


def test_viewer_cannot_create_users(db_path):
    bootstrap_admin(db_path, 1001)
    create_user(db_path, 1001, 2002)

    with pytest.raises(PermissionError):
        create_user(db_path, 2002, 3003)


def test_missing_and_disabled_users_are_denied(db_path):
    bootstrap_admin(db_path, 1001)

    with pytest.raises(PermissionError):
        require_permission(
            db_path, 9999, "status.read"
        )

    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE telegram_users SET enabled = 0 "
            "WHERE user_id=1001"
        )

    with pytest.raises(PermissionError):
        require_permission(
            db_path, 1001, "status.read"
        )


def test_invalid_ids_and_roles(db_path):
    for bad in (0, -1, True, "1001", 2**63):
        with pytest.raises(ValueError):
            bootstrap_admin(db_path, bad)

    bootstrap_admin(db_path, 1001)

    with pytest.raises(ValueError):
        create_user(
            db_path, 1001, 5, role="admin"
        )


def test_duplicate_and_unknown_permission(db_path):
    bootstrap_admin(db_path, 1001)
    create_user(db_path, 1001, 2002)

    with pytest.raises(sqlite3.IntegrityError):
        create_user(db_path, 1001, 2002)

    with pytest.raises(ValueError):
        require_permission(
            db_path, 1001, "root.everything"
        )
