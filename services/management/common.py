"""Спільне для керування: конфлікт версій і відбиток стану."""
import hashlib
import json


class ManagementConflict(Exception):
    pass


def version(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
