"""Bounded Telegram audio download; bot URLs and file IDs stay server-side."""

from pathlib import PurePosixPath
import re
import time

import requests

MAX_AUDIO_BYTES = 2 * 1024 * 1024


class AudioUnavailable(Exception):
    pass


def download_voice(token, file_id):
    try:
        meta = requests.get(f"https://api.telegram.org/bot{token}/getFile",
                            params={"file_id": file_id}, timeout=(2, 3), allow_redirects=False)
        meta.raise_for_status()
        payload = meta.json()
        result = payload.get("result", {})
        path = result.get("file_path", "")
        if (payload.get("ok") is not True or not isinstance(path, str) or not path
                or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts
                or any(c in path for c in ("?", "#", "\\", ":"))
                or not re.fullmatch(r"[A-Za-z0-9_./-]+", path)
                or result.get("file_size", 0) > MAX_AUDIO_BYTES):
            raise AudioUnavailable()
        deadline = time.monotonic() + 5
        with requests.get(f"https://api.telegram.org/file/bot{token}/{path}",
                          stream=True, timeout=(2, 3), allow_redirects=False) as response:
            response.raise_for_status()
            if response.status_code != 200:
                raise AudioUnavailable()
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > MAX_AUDIO_BYTES or time.monotonic() > deadline:
                    raise AudioUnavailable()
                chunks.append(chunk)
        audio = b"".join(chunks)
        if not audio.startswith(b"OggS"):
            raise AudioUnavailable()
        return audio
    except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError):
        raise AudioUnavailable() from None
