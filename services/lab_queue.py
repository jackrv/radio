"""Черга офлайн-тестів лабораторії, якою керує панель.

Тест — готовий рецепт (``PRESETS``) з описом простими словами: один чинник
змінено відносно «як зараз у приймачі». Черга — файл ``queue.json`` у
.local-experiments; виконавець — окремий процес (``python -m services.lab_queue
run``), який переживає перезапуск служб:

- перед важким тестом сам зупиняє приймач через лаунчер (Whisper large-v3 і
  приймач разом не вміщаються в пам'ять Mac) і знову запускає його наприкінці —
  навіть після помилки чи зупинки;
- між тестами тримає паузу, щоб Mac не перегрівався;
- може чекати години старту («о 01:00»).

Архів і бази тест лише читає (див. tools/asr_lab).
"""
import argparse
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COOLDOWN_S = 300          # пауза між важкими тестами (перегрів Mac)
RECEIVER_WAIT_S = 120     # скільки чекати, поки лаунчер зупинить приймач
MAX_JOBS = 30

# Спільне для всіх рецептів: повний шлях, як в ефірі (Whisper + обробка тексту), лише розмічені записи.
BASE = ["--variant", "pipeline", "--prompt", "receiver", "--daily-codes", "latest", "--retry", "on", "--llm", "off", "--labelled-only"]


def _with(base, **changes):
    """BASE зі зміненими прапорцями: _with(BASE, prompt="none")."""
    args = list(base)
    for key, value in changes.items():
        flag = "--" + key.replace("_", "-")
        if flag in args:
            args[args.index(flag) + 1] = value
        else:
            args += [flag, value]
    return args


PRESETS = {
    "base": {"title": "Як зараз у приймачі",
             "about": "Еталон для порівняння: увесь шлях, як в ефірі — Whisper із підказкою (позивні й останній код доби), "
                      "другий шанс при зацикленні, обробка тексту. Запускайте першим і після змін словників, підказки чи налаштувань.",
             "args": BASE, "minutes": 30},
    "no-prompt": {"title": "Без підказки Whisper",
                  "about": "Whisper не отримує списку позивних і кодів. Покаже, чи підказка допомагає, чи змушує вигадувати позивні.",
                  "args": _with(BASE, prompt="none", daily_codes="off"), "minutes": 30},
    "no-codes": {"title": "Без коду доби в підказці",
                 "about": "Підказка лише з позивними, без останнього коду доби. Чи допомагає код доби правильно чути числа.",
                 "args": _with(BASE, daily_codes="off"), "minutes": 30},
    "no-retry": {"title": "Без другого шансу",
                 "about": "Не повторювати розпізнавання без підказки, коли Whisper зациклився. Скільки фраз рятує другий шанс.",
                 "args": _with(BASE, retry="off"), "minutes": 25},
    "bandpass": {"title": "Фільтр частот мовлення",
                 "about": "Перед Whisper прибрати низький гул і високе шипіння (лишити смугу голосу). Може допомогти на шумному ефірі.",
                 "args": _with(BASE, bandpass="on"), "minutes": 30},
    "resegment": {"title": "Повторна нарізка запису",
                  "about": "Різати запис на шматки з урахуванням гучності над фоном. Може зменшити вигадані слова на тиші між передачами.",
                  "args": _with(BASE, resegment="energy"), "minutes": 35},
    "turbo": {"title": "Швидша модель (turbo)",
              "about": "Whisper large-v3-turbo замість large-v3: у кілька разів швидша, зазвичай трохи гірша. Чи прийнятна якість.",
              "args": _with(BASE, model="mlx-community/whisper-large-v3-turbo"), "minutes": 15},
}
_NOTE = re.compile(r"[\w\s.,:;!?()«»'’ʼ\"/+-]{0,200}\Z")


def queue_dir(root=None):
    """Тека експериментів (там і черга); без аргументу — .local-experiments проєкту."""
    path = Path(root) if root else ROOT / ".local-experiments"
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def _file(root):
    return queue_dir(root) / "queue.json"


@contextmanager
def _locked(root):
    """Читання-зміна-запис черги під замком (панель і виконавець пишуть по черзі)."""
    with open(queue_dir(root) / "queue.lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            path = _file(root)
            try:
                state = json.loads(path.read_text())
            except (OSError, ValueError):
                state = {}
            state.setdefault("jobs", [])
            state.setdefault("runner", {})
            yield state
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1))
            os.replace(tmp, path)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def read(root=None):
    with _locked(root) as state:
        return json.loads(json.dumps(state))


def add(root, preset, note=""):
    if preset not in PRESETS:
        raise ValueError("Невідомий тест")
    note = (note or "").strip()
    if not _NOTE.fullmatch(note):
        raise ValueError("Примітка — до 200 знаків, без спецсимволів")
    with _locked(root) as state:
        if sum(j["status"] == "queued" for j in state["jobs"]) >= MAX_JOBS:
            raise ValueError(f"У черзі вже {MAX_JOBS} тестів")
        job = {"id": uuid.uuid4().hex[:12], "preset": preset, "note": note, "status": "queued", "added": time.time()}
        state["jobs"].append(job)
        return job


def move(root, job_id, delta):
    with _locked(root) as state:
        queued = [j for j in state["jobs"] if j["status"] == "queued"]
        job = next((j for j in queued if j["id"] == job_id), None)
        if job is None:
            raise LookupError("Тест не в черзі")
        index = queued.index(job)
        target = max(0, min(len(queued) - 1, index + int(delta)))
        queued.insert(target, queued.pop(index))
        others = [j for j in state["jobs"] if j["status"] != "queued"]
        state["jobs"] = others + queued


def cancel(root, job_id):
    """Тест у черзі — прибрати; тест, що йде, — зупинити (виконавець перейде до наступного)."""
    with _locked(root) as state:
        job = next((j for j in state["jobs"] if j["id"] == job_id), None)
        if job is None:
            raise LookupError("Тест не знайдено")
        if job["status"] == "queued":
            state["jobs"].remove(job)
            return "removed"
        if job["status"] == "running":
            job["cancel"] = True
            _kill(job.get("pid"))
            return "stopping"
        state["jobs"].remove(job)   # завершений — прибрати з переліку
        return "removed"


def clear_finished(root):
    """Прибрати з переліку лише успішно виконані; перервані й невдалі лишаються (їх можна повторити)."""
    with _locked(root) as state:
        state["jobs"] = [j for j in state["jobs"] if j["status"] != "done"]


def retry(root, job_id):
    """Перерваний чи невдалий тест — знову в чергу (той самий рецепт і примітка), старий рядок прибирається."""
    with _locked(root) as state:
        job = next((j for j in state["jobs"] if j["id"] == job_id), None)
        if job is None or job["status"] not in ("cancelled", "failed"):
            raise LookupError("Повторити можна лише перерваний чи невдалий тест")
        state["jobs"].remove(job)
        state["jobs"].append({"id": uuid.uuid4().hex[:12], "preset": job["preset"], "note": job.get("note", ""),
                              "status": "queued", "added": time.time()})


def request_stop(root, now=False):
    """Зупинити виконавця після поточного тесту (або одразу — now=True)."""
    with _locked(root) as state:
        state["runner"]["stop"] = True
        if now:
            for job in state["jobs"]:
                if job["status"] == "running":
                    job["cancel"] = True
                    _kill(job.get("pid"))


# Відносна тривалість рецепта (до «як зараз»): turbo швидша, повторна нарізка — довша.
DURATION_FACTOR = {"turbo": 0.4, "resegment": 1.2}
SECONDS_PER_RECORD = 4.0      # якщо минулих прогонів немає (18.11.2026: 2,9–4,0 с на запис)
WINDOW_MARGIN = 1.15          # запас до кінця вікна


def set_windows(root, windows):
    """Вікна часу [[початок, кінець], …] (unix-час), коли можна зупиняти приймач; поза ними він працює."""
    clean = sorted([float(a), float(b)] for a, b in (windows or []) if float(b) > float(a))
    with _locked(root) as state:
        state["runner"]["windows"] = clean or None


def estimate_seconds(root, preset, records):
    """Оцінка тривалості тесту: записів × секунд на запис з останніх завершених прогонів × множник рецепта."""
    per_record = []
    for folder in sorted((p for p in queue_dir(root).iterdir() if p.is_dir() and (p / "metrics.json").is_file()), reverse=True)[:6]:
        try:
            params = json.loads((folder / "params.json").read_text())
            began = time.mktime(time.strptime(params["started_utc"], "%Y%m%dT%H%M%SZ")) - time.timezone
            if params.get("records"):
                per_record.append(((folder / "results.jsonl").stat().st_mtime - began) / params["records"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
    rate = max(per_record) if per_record else SECONDS_PER_RECORD     # беремо повільніший — безпечніше для вікна
    return records * rate * DURATION_FACTOR.get(preset, 1.0)


def set_start_at(root, start_at):
    with _locked(root) as state:
        state["runner"]["start_at"] = float(start_at) if start_at else None


def _kill(pid):
    if isinstance(pid, int) and pid > 1:
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass


def _alive(pid):
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def runner_active(state):
    return _alive((state.get("runner") or {}).get("pid"))


def progress(root, job):
    """{done, total}: скільки записів уже пройдено в теці прогону цього тесту."""
    run_dir = job.get("run_dir")
    if not run_dir:
        return None
    folder = queue_dir(root) / run_dir
    try:
        with (folder / "results.jsonl").open(encoding="utf-8") as f:
            done = sum(1 for line in f if line.strip())
    except OSError:
        done = 0
    try:
        total = json.loads((folder / "params.json").read_text()).get("records")
    except (OSError, ValueError):
        total = None
    return {"done": done, "total": total}


def start(root=None, python=None, launcher_root=None):
    """Запустити виконавця окремим процесом (якщо ще не працює)."""
    with _locked(root) as state:
        if runner_active(state):
            return False
        state["runner"] = {**state["runner"], "stop": False}
    log = open(queue_dir(root) / "queue-runner.log", "a")
    subprocess.Popen([python or sys.executable, "-m", "services.lab_queue", "run", "--dir", str(queue_dir(root)),
                      "--launcher-root", str(launcher_root or ROOT)],
                     cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    return True


# ---------- Виконавець ----------
class Launcher:
    """Зупинка й запуск приймача через лаунчер (той самий канал, що й кнопки панелі)."""
    def __init__(self, root):
        self.root = root

    def receiver_stopped(self):
        from services.launcher_lifecycle import managed_status
        return "radio" in managed_status(self.root).get("stopped_services", [])

    def send(self, command):
        from services.launcher_lifecycle import send_command
        return send_command(self.root, command)


def _set(root, **runner):
    with _locked(root) as state:
        state["runner"].update(runner)


def _finish_job(root, job_id, **fields):
    with _locked(root) as state:
        for job in state["jobs"]:
            if job["id"] == job_id:
                job.update(fields)


def _run_dir_for(root, name, since):
    candidates = [p for p in queue_dir(root).glob(f"*_{name}") if p.is_dir() and p.stat().st_mtime >= since - 5]
    return max(candidates, key=lambda p: p.name).name if candidates else None


def run(root=None, launcher=None, spawn=None, sleep=time.sleep, now=time.time, cooldown=COOLDOWN_S, estimate=None):
    """Виконує тести по черзі, доки є що виконувати або не попросили зупинитись.

    root — тека експериментів (черга); лабораторія запускається з кореня проєкту.
    """
    root = queue_dir(root)
    launcher = launcher or Launcher(ROOT)
    if estimate is None:
        def estimate(preset):
            try:
                import config
                from services.recordings import RecordingStore
                with RecordingStore(Path(config.RECORDINGS_DIR)).connect() as db:
                    records = db.execute("SELECT COUNT(*) FROM recordings WHERE reference IS NOT NULL").fetchone()[0]
            except Exception:
                records = 1000
            return estimate_seconds(root, preset, records)
    spawn = spawn or (lambda args, log: subprocess.Popen(args, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
                                                         stdin=subprocess.DEVNULL, start_new_session=True))
    _set(root, pid=os.getpid(), started=now(), state="starting")   # «stop» скидає лише start()
    stopped_by_us, last_finish = False, None
    try:
        while True:
            state = read(root)
            runner = state["runner"]
            if runner.get("stop"):
                break
            queued = [j for j in state["jobs"] if j["status"] == "queued"]
            if not queued:
                break
            job = queued[0]
            # Вікна часу: приймач зупиняємо лише всередині вікна, і лише для тесту, що встигне до його кінця.
            windows = runner.get("windows")
            window_end = None
            if windows:
                inside = next((w for w in windows if w[0] <= now() < w[1]), None)
                fits = [j for j in queued if inside and now() + estimate(j["preset"]) * WINDOW_MARGIN <= inside[1]] if inside else []
                if not fits:
                    following = [w for w in windows if w[0] > now()]
                    if stopped_by_us:   # поза вікном (чи нічого не встигне) — приймач працює
                        launcher.send("receiver-start")
                        stopped_by_us = False
                    if not following:
                        break
                    _set(root, state="waiting", until=following[0][0])
                    sleep(min(30, following[0][0] - now()))
                    continue
                job, window_end = fits[0], inside[1]
            start_at = runner.get("start_at")
            if start_at and now() < start_at:
                _set(root, state="waiting", until=start_at)
                sleep(min(30, start_at - now()))
                continue
            if last_finish is not None and now() - last_finish < cooldown:
                _set(root, state="cooldown", until=last_finish + cooldown)
                sleep(min(30, last_finish + cooldown - now()))
                continue
            if not stopped_by_us and not launcher.receiver_stopped():
                _set(root, state="stopping_receiver")
                launcher.send("receiver-stop")
                stopped_by_us = True
                deadline = now() + RECEIVER_WAIT_S
                while not launcher.receiver_stopped() and now() < deadline:
                    sleep(2)
                if not launcher.receiver_stopped():
                    _finish_job(root, job["id"], status="failed", error="Приймач не зупинився — тест не запущено")
                    break
            preset = PRESETS[job["preset"]]
            name = f"q-{job['preset']}"
            logs = queue_dir(root) / "queue-logs"
            logs.mkdir(exist_ok=True)
            began = now()
            with open(logs / f"{job['id']}.log", "a") as log:
                process = spawn([sys.executable, "-m", "tools.asr_lab", "run", "--name", name, *preset["args"]], log)
                _finish_job(root, job["id"], status="running", pid=process.pid, started=began)
                _set(root, state="running", current=job["id"], until=None)
                while process.poll() is None:
                    sleep(5)
                    if window_end and now() >= window_end - 20:   # вікно закінчується — приймач важливіший
                        _finish_job(root, job["id"], cancel=True, error="перервано: закінчилось вікно часу")
                        _kill(process.pid)
                        process.wait() if hasattr(process, "wait") else None
                    run_dir = _run_dir_for(root, name, began)
                    if run_dir:
                        _finish_job(root, job["id"], run_dir=run_dir)
            current = next((j for j in read(root)["jobs"] if j["id"] == job["id"]), {})
            status = "cancelled" if current.get("cancel") else "done" if process.returncode == 0 else "failed"
            _finish_job(root, job["id"], status=status, finished=now(), returncode=process.returncode, pid=None,
                        run_dir=_run_dir_for(root, name, began))
            last_finish = now()
    finally:
        if stopped_by_us:
            try:
                launcher.send("receiver-start")
            except Exception:   # приймач не лишаємо зупиненим через збій каналу — повтор нижче
                sleep(5)
                launcher.send("receiver-start")
        _set(root, pid=None, state="idle", current=None, until=None, stop=False, start_at=None, windows=None, finished=now())


def main(argv=None):
    parser = argparse.ArgumentParser(description="Черга офлайн-тестів лабораторії")
    sub = parser.add_subparsers(dest="command", required=True)
    runner = sub.add_parser("run", help="виконати чергу (запускає панель)")
    runner.add_argument("--dir", default=None, help="тека експериментів (типово — .local-experiments)")
    runner.add_argument("--launcher-root", default=str(ROOT))
    args = parser.parse_args(argv)
    if args.command == "run":
        root = queue_dir(args.dir)
        with open(root / "queue-runner.lock", "a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("Виконавець черги вже працює")
                return 1
            run(root, launcher=Launcher(Path(args.launcher_root)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
