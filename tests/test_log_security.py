import logging
import sys

from core.log_security import RedactingFormatter


TOKEN = "123456:FAKE_SECRET_TOKEN"


def make_formatter():
    return RedactingFormatter(
        "%(levelname)s %(message)s",
        None,
        TOKEN,
    )


def test_token_hidden_in_request_url():
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    record = logging.LogRecord(
        "test",
        logging.ERROR,
        __file__,
        1,
        "HTTP error: %s",
        (url,),
        None,
    )

    result = make_formatter().format(record)

    assert TOKEN not in result
    assert "[REDACTED]" in result


def test_token_hidden_in_traceback():
    try:
        raise RuntimeError(
            f"400 Client Error: "
            f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        )
    except RuntimeError:
        error_info = sys.exc_info()

    record = logging.LogRecord(
        "test",
        logging.ERROR,
        __file__,
        1,
        "Помилка Telegram",
        (),
        error_info,
    )

    result = make_formatter().format(record)

    assert TOKEN not in result
    assert "[REDACTED]" in result
    assert "RuntimeError" in result
