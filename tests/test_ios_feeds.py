"""Логіка двох джерел iOS (Feeds.swift) без симулятора: потрібен swiftc, інакше пропускається."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not shutil.which("swiftc"), reason="немає swiftc")
def test_feeds_logic(tmp_path):
    binary = tmp_path / "check"
    built = subprocess.run(["swiftc", "-parse-as-library", "-o", str(binary), str(ROOT / "apps/ios/Radio345/Sources/Feeds.swift"),
                            str(ROOT / "tests/ios_feeds_check.swift")], capture_output=True, text=True, timeout=300)
    assert built.returncode == 0, built.stderr[-1500:]
    done = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and "FAIL" not in done.stdout and done.stdout.count("ok ") == 18, done.stdout
