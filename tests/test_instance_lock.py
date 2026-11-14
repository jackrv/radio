"""Один приймач: друге блокування не видається, після закриття — видається знову."""

import os
import subprocess
import sys

from core.instance_lock import acquire_instance_lock


def test_second_holder_is_refused_and_lock_released_on_close(tmp_path):
    path = tmp_path / ".radio345.lock"
    first = acquire_instance_lock(path)
    assert first is not None
    assert acquire_instance_lock(path) is None
    os.close(first)
    again = acquire_instance_lock(path)
    assert again is not None
    os.close(again)


def test_lock_held_by_another_process_and_freed_when_it_dies(tmp_path):
    path = tmp_path / ".radio345.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys,time; from core.instance_lock import acquire_instance_lock as a;"
         f"fd=a({str(path)!r}); print('locked' if fd is not None else 'no', flush=True); time.sleep(30)"],
        stdout=subprocess.PIPE, text=True, cwd=os.getcwd(),
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        assert acquire_instance_lock(path) is None, "одночасний другий приймач відхилено"
    finally:
        holder.kill()
        holder.wait()
    fd = acquire_instance_lock(path)
    assert fd is not None, "після аварійного завершення блокування зняте ОС"
    os.close(fd)
