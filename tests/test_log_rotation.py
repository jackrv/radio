import logging
from pathlib import Path

import core.log_security as security


def test_archive_names():
    name = security.archive_log_name

    assert name("/tmp/radio.log.1") == (
        "/tmp/radio-1.log"
    )

    assert name("/tmp/radio.log.2") == (
        "/tmp/radio-2.log"
    )

    assert name("/tmp/radio.log.5") == (
        "/tmp/radio-5.log"
    )

    assert name("/tmp/radio.log") == (
        "/tmp/radio.log"
    )


def test_rotation_masks_token(tmp_path, monkeypatch):
    path = tmp_path / "radio.log"

    monkeypatch.setattr(security, "LOG_PATH", path)
    monkeypatch.setattr(security, "LOG_MAX_BYTES", 250)
    monkeypatch.setattr(security, "LOG_BACKUP_COUNT", 2)

    captured = {}

    monkeypatch.setattr(
        security.logging,
        "basicConfig",
        lambda **kwargs: captured.update(kwargs),
    )

    security.configure_logging("SECRET_TEST_TOKEN")

    handlers = captured["handlers"]
    handler = handlers[0]

    try:
        for index in range(60):
            record = logging.LogRecord(
                name="radio",
                level=logging.ERROR,
                pathname=__file__,
                lineno=index + 1,
                msg=(
                    "https://api.telegram.org/"
                    "botSECRET_TEST_TOKEN/sendMessage "
                    f"ошибка {index}"
                ),
                args=(),
                exc_info=None,
            )

            handler.handle(record)

    finally:
        for item in handlers:
            item.close()

    assert path.exists()
    assert (tmp_path / "radio-1.log").exists()
    assert (tmp_path / "radio-2.log").exists()

    files = list(tmp_path.glob("radio*.log"))

    assert len(files) == 3

    for file in files:
        content = file.read_text(encoding="utf-8")

        assert file.stat().st_size <= 250
        assert "SECRET_TEST_TOKEN" not in content
        assert "[REDACTED]" in content
