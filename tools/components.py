"""Карта компонентів: процеси, їхні вхідні модулі й межі імпортів каталогів.

    .venv/bin/python -m tools.components          # процеси, розміри, каталоги
    .venv/bin/python -m tools.components --list shared

Статичний аналіз AST; динамічні імпорти не видно. Межі перевіряє tests/test_components.py.
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Процес → (майбутній каталог, вхідні модулі).
PROCESSES = {
    "node": ("server", ("server.radio_node", "server.segment_spool", "server.node_control", "server.phrase_feed")),
    "asr": ("server", ("server.asr_service",)),
    "panel": ("server", ("server.api.local_dashboard",)),
    "api": ("server", ("server.api.app", "server.api.local_server")),
    "tunnel": ("server", ("server.miniapp_tunnel",)),
    "worker": ("worker", ("worker.asr_worker",)),
    "lab": ("lab", ("lab.asr_lab", "lab.whisper_finetune", "lab.text_replay", "lab.analysis")),
}
# Не входять до жодного нового процесу: нинішній моноліт Mac, обслуговування, разові інструменти.
LEGACY_ENTRIES = ("radio345", "tools.manage_database", "tools.diagnose")
FOLDERS = ("shared", "server", "worker", "lab")
MLX = ("mlx", "mlx_whisper", "mlx_lm")


def modules(root=ROOT):
    result = {}
    for top in ("shared", "server", "worker", "lab", "tools"):
        for path in sorted((root / top).rglob("*.py")):
            if "__pycache__" not in path.parts:
                result[".".join(path.relative_to(root).with_suffix("").parts).removesuffix(".__init__")] = path
    for path in sorted(root.glob("*.py")):
        result[path.stem] = path
    return result


def _imports(node, module, is_package):
    if isinstance(node, ast.Import):
        return [a.name for a in node.names]
    base = node.module or ""
    if node.level:
        parts = module.split(".") if is_package else module.split(".")[:-1]
        parts = parts[:len(parts) - node.level + 1]
        base = ".".join([*parts, base] if base else parts)
    return [base, *(f"{base}.{a.name}" for a in node.names)]


def graph(root=ROOT, eager_only=False):
    """(проєктні залежності, зовнішні пакети). eager_only — без імпортів усередині функцій."""
    known = modules(root)
    tops = {m.split(".")[0] for m in known}
    inner = {m: set() for m in known}
    outer = {m: set() for m in known}
    for name, path in known.items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        lazy = set()
        if eager_only:
            for fn in ast.walk(tree):
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    lazy.update(id(n) for n in ast.walk(fn) if n is not fn)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and id(node) not in lazy:
                for target in _imports(node, name, path.name == "__init__.py"):
                    if target in known:
                        inner[name].add(target)
                    elif target and target.split(".")[0] not in tops:
                        outer[name].add(target.split(".")[0])
        inner[name].discard(name)
    return inner, outer


def closure(entries, inner):
    seen, stack = set(entries), list(entries)
    while stack:
        for nxt in inner[stack.pop()] - seen:
            seen.add(nxt)
            stack.append(nxt)
    return seen


def process_closures(inner):
    return {name: closure(entries, inner) for name, (_, entries) in PROCESSES.items()}


def layout(inner):
    """Каталог модуля; решта — legacy: моноліт Mac, обслуговування, разові інструменти. config.py — спільний."""
    return {m: "shared" if m == "config" else m.split(".")[0] if m.split(".")[0] in FOLDERS else "legacy" for m in inner}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    inner, outer = graph()
    placed = layout(inner)
    if "--list" in argv:
        folder = argv[argv.index("--list") + 1]
        print("\n".join(sorted(m for m, f in placed.items() if f == folder)))
        return 0
    for name, ms in process_closures(inner).items():
        print(f"{name:7} {len(ms):3} модулів")
    for folder in ("shared", "server", "worker", "lab", "legacy"):
        print(f"{folder + '/':8} {sum(f == folder for f in placed.values()):3}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
