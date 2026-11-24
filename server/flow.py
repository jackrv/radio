"""Жива схема панелі: рух даних у «домі» раз на секунду. Лише лічильники й прапорці — без текстів і звуку.

Окремий процес (python3 на цій машині або через SSH на Pi) друкує рядки JSON, доки схему дивляться.
"""
import json
import subprocess
import sys
import threading
import time

import config
from shared import home_marker
from shared.paths import REMOTE_PLACE

IDLE_S = 15.0      # схему закрили — знімки припиняються
RETRY_S = 5.0
PORT = 8346        # config.RADIO_STREAM_PORT

PROBE = REMOTE_PLACE + r'''
import json, os, shutil, sqlite3, subprocess, sys, time, urllib.request
root, port = os.path.expanduser(sys.argv[1]), sys.argv[2]
cli = shutil.which("tailscale") or "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def address():
    try:
        return subprocess.run([cli, "ip", "-4"], capture_output=True, text=True, timeout=5).stdout.split()[0]
    except (OSError, subprocess.SubprocessError, IndexError):
        return None

def scalar(sql):
    try:
        d = sqlite3.connect("file:%s?mode=ro" % place(root, "events.db"), uri=True, timeout=1)
        try:
            return d.execute(sql).fetchone()[0]
        finally:
            d.close()
    except sqlite3.Error:
        return None

ip, checked = address(), time.monotonic()
for _ in range(1800):
    out = {"node": None}
    try:
        with open(os.path.join(place(root, ".local-node"), "state.json")) as f:
            state = json.load(f)
        if time.time() - state["at"] < 10:
            out["node"] = state["state"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    out["whisper"] = None
    try:
        with open(os.path.join(place(root, ".local-node"), "whisper.json")) as f:
            w = json.load(f)
        out["whisper"] = {"state": w["state"], "sent": w["sent"], "age_s": max(0, time.time() - w["at"])}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        names = sorted(n for n in os.listdir(place(root, ".local-segment-spool")) if n.endswith(".seg"))
    except OSError:
        names = []
    out["queue"], out["segment"] = len(names), names[-1] if names else None
    out["event"] = scalar("SELECT MAX(id) FROM radio_events")
    try:
        out["min_rms"] = json.loads(scalar("SELECT values_json FROM runtime_settings WHERE id = 1") or "{}").get("MIN_RMS")
    except ValueError:
        out["min_rms"] = None
    out["sent"] = scalar("SELECT MAX(rowid) FROM telegram_deliveries")
    if ip is None and time.monotonic() - checked > 15:
        ip, checked = address(), time.monotonic()
    try:
        if ip:
            # after — далеко попереду: потрібен лише стан, без фраз.
            with opener.open("http://%s:%s/events?after=999999999999" % (ip, port), timeout=2) as r:
                live = json.load(r)
            out["air"], out["transmitting"] = live.get("air"), bool(live.get("transmitting"))
            out["listeners"] = int(live.get("listeners") or 0)
            level = live.get("level")
            if isinstance(level, dict):
                out["level"] = {k: level[k] for k in ("rms", "peak", "phrase_rms", "phrase_age_s")
                                if isinstance(level.get(k), (int, float))}
                out["level"]["louder"] = level.get("louder") or []
    except (OSError, ValueError, TypeError):
        pass
    print(json.dumps(out), flush=True)
    time.sleep(1)
'''


class Flow:
    def __init__(self, root, host=None, folder="radio345-hub", popen=subprocess.Popen, clock=time.monotonic):
        self.root, self.host, self.folder, self.popen, self.clock = root, host, folder, popen, clock
        self.lock = threading.Lock()
        self._process, self._target = None, None
        self._data, self._at = None, float("-inf")
        self._asked, self._started = float("-inf"), float("-inf")

    def target(self):
        marker = home_marker.read(self.root)
        home = marker["home"] if marker else home_marker.this_device()
        return home if home == home_marker.this_device() or (home == "pi" and self.host) else None

    def command(self, target):
        if target == home_marker.this_device():
            return [sys.executable, "-I", "-", str(self.root), str(PORT)]
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=5",
                "-o", "ServerAliveCountMax=2", "--", self.host, f"python3 -I - '~/{self.folder}' {PORT}"]

    def read(self):
        """Останній знімок, не чекаючи пристрою; age_s — скільки йому секунд."""
        target, now = self.target(), self.clock()
        with self.lock:
            self._asked = now
            if target != self._target:
                self._stop()
                self._target, self._data, self._started = target, None, float("-inf")
            if target and self._process is None and now - self._started >= RETRY_S:
                self._start(target, now)
            return {"home": target, "data": self._data, "age_s": None if self._data is None else now - self._at}

    def _start(self, target, now):
        self._started = now
        try:
            process = self.popen(self.command(target), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True)
        except OSError:
            return
        self._process = process
        threading.Thread(target=self._follow, args=(process,), name="panel-flow", daemon=True).start()

    def _stop(self):
        process, self._process = self._process, None
        if process is not None:
            process.terminate()

    def _follow(self, process):
        try:
            process.stdin.write(PROBE)
            process.stdin.close()
            for line in process.stdout:
                data = json.loads(line)
                with self.lock:
                    if self._process is not process:
                        break
                    if data.get("min_rms") is None:
                        data["min_rms"] = config.MIN_RMS   # у базі лише змінене в панелі
                    self._data, self._at = data, self.clock()
                    if self._at - self._asked > IDLE_S:
                        break
        except (OSError, ValueError):
            pass
        finally:
            process.terminate()
            with self.lock:
                if self._process is process:
                    self._process = None
            process.stdout.close()
            process.wait()
