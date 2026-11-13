def retry_delay(attempt: int, error) -> int:
    """Задержка перед повтором запроса Telegram."""
    delay = min(2 ** attempt, 60)

    response = getattr(error, "response", None)

    if response is None or response.status_code != 429:
        return delay

    try:
        data = response.json()
        retry_after = (data.get("parameters") or {}).get(
            "retry_after", 0
        )
    except (ValueError, TypeError, AttributeError):
        return delay

    if (
        isinstance(retry_after, int)
        and not isinstance(retry_after, bool)
        and retry_after > 0
    ):
        return max(delay, retry_after)

    return delay
