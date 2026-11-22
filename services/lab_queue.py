"""Черга офлайн-тестів лабораторії, якою керує панель.

Тест — готовий рецепт (``PRESETS``) з описом простими словами: один чинник
змінено відносно «як зараз у приймачі». Черга — файл ``queue.json`` у
.local-experiments; виконавець — окремий процес (``python -m services.lab_queue
run``), який переживає перезапуск служб:

- перед важким тестом переводить приймач у режим «лише запис»: він далі слухає
  та зберігає фрагменти, а Whisper вивантажено; після тестів модель відновлюється;
- між тестами тримає паузу, щоб Mac не перегрівався;
- може чекати години старту («о 01:00»);
- тест можна поставити на паузу (вручну чи в кінці вікна часу) і продовжити з того
  самого запису: результати пишуться порядково, перелік записів зафіксовано (``--resume``).

Сам тест (``tools.asr_lab``) читає розмічені записи; приймач паралельно додає нові
фрагменти до архіву, щоб розпізнати їх після відновлення Whisper.
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
RECEIVER_WAIT_S = 120     # скільки чекати, поки приймач вивантажить / відновить Whisper
CAPTURE_LEASE_S = 4 * 60 * 60
CAPTURE_RENEW_BEFORE_S = 5 * 60
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


# Лише актуальні рецепти; вирішені досліди (без підказки, без коду доби, turbo, bandpass тощо)
# прибрано 22.11.2026 — висновки в docs/lab-details.md і docs/ideas.md.
PRESETS = {
    "base": {"title": "Як зараз у приймачі",
             "about": "Еталон для порівняння: увесь шлях, як в ефірі — Whisper із підказкою (позивні й останній код доби), "
                      "другий шанс при зацикленні, обробка тексту. Запускайте першим і після змін словників, підказки чи налаштувань.",
             "args": BASE, "minutes": 30},
    "dictionary-blocks": {"title":"Повні команди зі словника у підказці",
                          "about":"Передати Whisper не тільки назви, а й повні словникові коди та групування. Порівняти з базою на тих самих записах; окремо перевірити вигадані команди на шумі/сигналах/кімнаті. Живий prompt не змінюється без перевірки.",
                          "args":_with(BASE,prompt="receiver-command-codes"),"minutes":30},
    "word-silence": {
        "title": "Whisper: слова й довгі паузи",
        "about": "Експеримент: послівні часові мітки й штатна перевірка довгих пауз Whisper (2 с). Додаткове вирівнювання може сповільнити ASR. Порівняти з базою на спільних еталонах, окремо пропуски мовлення та вигаданий текст на шумі/сигналах/кімнаті. У приймачі не ввімкнено.",
        "args": [*BASE, "--whisper-option", "word_timestamps=true",
                 "--whisper-option", "hallucination_silence_threshold=2"],
        "minutes": 30,
    },
    "resegment": {"title": "Повторна нарізка запису",
                  "about": "Різати запис на шматки з урахуванням гучності над фоном. Може зменшити вигадані слова на тиші між передачами.",
                  "args": _with(BASE, resegment="energy"), "minutes": 35},
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


def resumable(root, job):
    """Тест можна продовжити: є тека прогону з результатами, але без підсумку."""
    run_dir = job.get("run_dir") or job.get("resume_dir")
    if not run_dir or job["status"] not in ("paused", "failed", "cancelled"):
        return False
    folder = queue_dir(root) / run_dir
    return (folder / "results.jsonl").is_file() and (folder / "params.json").is_file() and not (folder / "metrics.json").is_file()


def pause(root, job_id):
    """Пауза тесту, що йде: процес зупиняється, черга теж; розпізнавання ефіру відновлюється.

    Готові записи лишаються в теці прогону — «Продовжити» дорахує решту.
    """
    with _locked(root) as state:
        job = next((j for j in state["jobs"] if j["id"] == job_id), None)
        if job is None or job["status"] != "running":
            raise LookupError("Поставити на паузу можна лише тест, що йде")
        job["pause"] = True
        state["runner"]["stop"] = True
        _kill(job.get("pid"))


def resume(root, job_id):
    """Призупинений (чи перерваний/невдалий) тест — на початок черги з тієї самої теки прогону."""
    with _locked(root) as state:
        job = next((j for j in state["jobs"] if j["id"] == job_id), None)
        if job is None or not resumable(root, job):
            raise LookupError("Продовжити можна лише незавершений тест із збереженими результатами")
        job.update(status="queued", resume_dir=job.get("run_dir") or job["resume_dir"])
        for key in ("pause", "cancel", "error", "returncode", "finished", "pid"):
            job.pop(key, None)
        state["jobs"].remove(job)
        first = next((i for i, j in enumerate(state["jobs"]) if j["status"] == "queued"), len(state["jobs"]))
        state["jobs"].insert(first, job)
        return job


def request_stop(root, now=False):
    """Зупинити виконавця після поточного тесту (або одразу — now=True)."""
    with _locked(root) as state:
        state["runner"]["stop"] = True
        if now:
            for job in state["jobs"]:
                if job["status"] == "running":
                    job["cancel"] = True
                    _kill(job.get("pid"))


# Відносна тривалість рецепта (до «як зараз»): повторна нарізка — довша.
DURATION_FACTOR = {"resegment": 1.2}
SECONDS_PER_RECORD = 4.0      # якщо минулих прогонів немає (18.11.2026: 2,9–4,0 с на запис)
WINDOW_MARGIN = 1.15          # запас оцінки тривалості (план і сповіщення)
WINDOW_END_S = 20             # тест стає на паузу за стільки секунд до кінця вікна
MIN_SLICE_S = 10 * 60         # коротший залишок вікна не варто перемикання приймача


def window_usable(start, end):
    """Одна умова для плану панелі й реального виконавця: у вікні ще є час на шматок тесту."""
    return start + MIN_SLICE_S <= end - WINDOW_END_S


def remaining_fraction(root, job):
    """Частка тесту, що лишилась: продовження рахує лише недороблені записи."""
    if not job.get("resume_dir"):
        return 1.0
    done = progress(root, {"run_dir": job["resume_dir"]}) or {}
    if not done.get("total"):
        return 1.0
    return max(0.0, 1 - done["done"] / done["total"])


def plan_windows(windows, jobs, estimates, *, now, cooldown=COOLDOWN_S, root=None):
    """Розклад по вікнах: тест, що не встигає, стає на паузу й продовжується в наступному вікні."""
    left = [{"id": j["id"], "preset": j["preset"],
             "seconds": estimates[j["preset"]] * (remaining_fraction(root, j) if root else 1.0) * WINDOW_MARGIN}
            for j in jobs if j["status"] == "queued"]
    plan, last_finish = [], None
    for start, end in windows:
        slot = {"window": [start, end], "jobs": []}
        at = max(start, now, last_finish + cooldown if last_finish is not None else start)
        while left and window_usable(at, end):
            job = left[0]
            take = min(job["seconds"], end - WINDOW_END_S - at)
            job["seconds"] -= take
            finished = job["seconds"] <= 1
            slot["jobs"].append({"job": {"id": job["id"], "preset": job["preset"]}, "start": at, "end": at + take,
                                 "partial": not finished})
            last_finish = at + take
            at = last_finish + cooldown
            if not finished:
                break
            left.pop(0)
        plan.append(slot)
    return {"plan": plan, "left": [{"id": j["id"], "preset": j["preset"]} for j in left],
            "cooldown_s": cooldown, "margin": WINDOW_MARGIN, "now": now}


def set_windows(root, windows):
    """Вікна часу [[початок, кінець], …] (unix-час), коли можна вмикати режим «лише запис»."""
    clean = sorted([float(a), float(b)] for a, b in (windows or []) if float(b) > float(a))
    with _locked(root) as state:
        state["runner"]["windows"] = clean or None


def estimate_seconds(root, preset, records):
    """Оцінка тривалості тесту: записів × секунд на запис з останніх завершених прогонів × множник рецепта."""
    from datetime import datetime, timezone
    import math
    per_record, matched = [], []
    for folder in sorted((p for p in queue_dir(root).iterdir() if p.is_dir() and (p / "metrics.json").is_file()), reverse=True)[:6]:
        try:
            params = json.loads((folder / "params.json").read_text())
            began = datetime.strptime(params["started_utc"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()
            if params.get("records") and not params.get("resumed_utc"):   # з паузами час на запис невідомий
                rate = ((folder / "results.jsonl").stat().st_mtime - began) / params["records"]
                if not math.isfinite(rate) or rate <= 0:
                    continue
                source_preset = folder.name.split("_q-", 1)[-1] if "_q-" in folder.name else None
                if source_preset == preset:
                    matched.append(rate)
                if source_preset in PRESETS:
                    per_record.append(rate / DURATION_FACTOR.get(source_preset, 1.0))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if matched:
        return records * max(matched)
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
    """Стан приймача через лаунчер (той самий канал, що й кнопки панелі)."""
    def __init__(self, root):
        self.root = root

    def receiver_stopped(self):
        from services.launcher_lifecycle import managed_status
        return "radio" in managed_status(self.root).get("stopped_services", [])

    def send(self, command):
        from services.launcher_lifecycle import send_command
        return send_command(self.root, command)


class CaptureOnly:
    """Керування режимом запису лише з фрагментами через ту саму базу, що й приймач."""
    def __init__(self, db_path):
        self.db_path = db_path

    def start(self, until):
        from services.receiver_control import set_capture_only
        set_capture_only(self.db_path, until)

    def renew(self, until):
        from services.receiver_control import set_capture_only
        set_capture_only(self.db_path, until, reset_unloaded=False)

    def ready(self):
        from services.receiver_control import capture_state
        state = capture_state(self.db_path)
        return state["active"] and state["unloaded"] is not None

    def stop(self):
        from services.receiver_control import set_capture_only
        set_capture_only(self.db_path, None)

    def restored(self):
        from services.receiver_control import capture_state
        state = capture_state(self.db_path)
        return not state["active"] and state["unloaded"] is None


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


def run(root=None, launcher=None, spawn=None, sleep=time.sleep, now=time.time, cooldown=COOLDOWN_S,
        estimate=None, capture=None, notify=None):
    """Виконує тести по черзі, доки є що виконувати або не попросили зупинитись.

    root — тека експериментів (черга); лабораторія запускається з кореня проєкту.
    """
    root = queue_dir(root)
    launcher = launcher or Launcher(ROOT)
    if capture is None:
        import config
        capture = CaptureOnly(config.DB_PATH)
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
    _set(root, pid=os.getpid(), started=now(), state="starting", capture_error=None)
    capture_by_us, capture_until, last_finish = False, 0.0, None

    def renew_capture():
        nonlocal capture_until
        if capture_by_us and now() + CAPTURE_RENEW_BEFORE_S >= capture_until:
            capture_until = now() + CAPTURE_LEASE_S
            capture.renew(capture_until)

    def stop_capture():
        nonlocal capture_by_us
        if not capture_by_us:
            return True
        _set(root, state="restoring_recognition")
        capture.stop()
        capture_by_us = False
        deadline = now() + RECEIVER_WAIT_S
        while not capture.restored() and now() < deadline:
            sleep(min(1, deadline - now()))
        restored = capture.restored()
        if not restored:
            _set(root, capture_error="Приймач не підтвердив відновлення Whisper")
        return restored

    def start_capture(job):
        nonlocal capture_by_us, capture_until
        if launcher.receiver_stopped():
            _finish_job(root, job["id"], status="failed", error="Приймач вимкнений — режим «лише запис» недоступний")
            return False
        _set(root, state="starting_capture")
        capture_until = now() + CAPTURE_LEASE_S
        capture.start(capture_until)
        capture_by_us = True
        deadline = now() + RECEIVER_WAIT_S
        while not capture.ready() and now() < deadline:
            if read(root)["runner"].get("stop"):
                return False
            sleep(min(1, deadline - now()))
        if not capture.ready():
            _finish_job(root, job["id"], status="failed", error="Приймач не вивантажив Whisper — тест не запущено")
            return False
        _set(root, state="capturing_audio")
        return True

    try:
        while True:
            state = read(root)
            runner = state["runner"]
            if runner.get("stop"):
                break
            renew_capture()
            queued = [j for j in state["jobs"] if j["status"] == "queued"]
            if not queued:
                break
            job = queued[0]
            # Вікна часу: режим «лише запис» лише всередині вікна; наприкінці вікна тест стає на паузу.
            windows = runner.get("windows")
            window_end = None
            if windows:
                inside = next((w for w in windows if w[0] <= now() < w[1]), None)
                if inside is None or not window_usable(now(), inside[1]):
                    following = [w for w in windows if w[0] > now()]
                    if capture_by_us and not stop_capture():   # поза вікном — звичайне розпізнавання
                        break
                    if not following:
                        break
                    _set(root, state="waiting", until=following[0][0])
                    sleep(min(30, following[0][0] - now()))
                    continue
                window_end = inside[1]
            start_at = runner.get("start_at")
            if start_at and now() < start_at:
                _set(root, state="waiting", until=start_at)
                sleep(min(30, start_at - now()))
                continue
            if last_finish is not None and now() - last_finish < cooldown:
                _set(root, state="cooldown", until=last_finish + cooldown)
                sleep(min(30, last_finish + cooldown - now()))
                continue
            if not capture_by_us and not start_capture(job):
                if capture_by_us:     # скасували під час очікування; режим вимикаємо в finally
                    break
                continue
            preset = PRESETS[job["preset"]]
            name = f"q-{job['preset']}"
            resume_from = job.get("resume_dir")
            if notify and (last_finish is None or resume_from):   # інші старти оголошує підсумок попереднього тесту
                try:
                    from services import lab_notify
                    left = sum(j["status"] == "queued" for j in read(root)["jobs"]) - 1
                    minutes = max(1, round(estimate(job["preset"]) * remaining_fraction(root, job) / 60))
                    notify(lab_notify.start_text(preset["title"], minutes, left, resumed=bool(resume_from)))
                except Exception:
                    pass
            logs = queue_dir(root) / "queue-logs"
            logs.mkdir(exist_ok=True)
            began = now()
            with open(logs / f"{job['id']}.log", "a") as log:
                command = [sys.executable, "-m", "tools.asr_lab", "run", "--name", name, *preset["args"]]
                if resume_from:
                    command += ["--resume", str(queue_dir(root) / resume_from)]
                process = spawn(command, log)
                _finish_job(root, job["id"], status="running", pid=process.pid, started=began)
                _set(root, state="running", current=job["id"], until=None)
                while process.poll() is None:
                    sleep(5)
                    renew_capture()
                    if window_end and now() >= window_end - WINDOW_END_S:   # вікно закінчується — приймач важливіший
                        _finish_job(root, job["id"], window_pause=True)
                        _kill(process.pid)
                        process.wait() if hasattr(process, "wait") else None
                    run_dir = resume_from or _run_dir_for(root, name, began)
                    if run_dir:
                        _finish_job(root, job["id"], run_dir=run_dir)
            current = next((j for j in read(root)["jobs"] if j["id"] == job["id"]), {})
            run_dir = resume_from or _run_dir_for(root, name, began)
            stopped = current.get("pause") or current.get("window_pause")
            status = ("cancelled" if current.get("cancel") else "done" if process.returncode == 0
                      else "paused" if stopped and run_dir else "failed")
            if status == "paused" and current.get("window_pause"):
                # Кінець вікна: тест лишається першим у черзі й продовжиться в наступному вікні.
                _finish_job(root, job["id"], status="queued", resume_dir=run_dir, run_dir=run_dir, pid=None,
                            window_pause=None, returncode=None)
                status = "window_paused"
            else:
                _finish_job(root, job["id"], status=status, finished=now(), returncode=process.returncode, pid=None,
                            run_dir=run_dir, pause=None, window_pause=None,
                            **({"resume_dir": run_dir} if status == "paused" else {}))
            last_finish = now()
            # Короткий підсумок адміністраторам; збій надсилання не зупиняє чергу.
            try:
                from services import lab_notify   # лише текст; надсилання — через notify (CLI-виконавець)
                text = lab_notify.summary_text(root, preset["title"], status, run_dir,
                                               max(1, round((last_finish - began) / 60)),
                                               progress=progress(root, {"run_dir": run_dir}) if run_dir else None)
                if notify:
                    notify(text)
            except Exception:
                pass
    finally:
        if capture_by_us:
            try:
                if not stop_capture():
                    _set(root, capture_error="Приймач не підтвердив відновлення Whisper")
            except Exception as exc:
                _set(root, capture_error=f"Не вдалося вимкнути режим «лише запис»: {exc}")
        _set(root, pid=None, state="idle", current=None, until=None, stop=False, start_at=None, windows=None,
             finished=now())


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
            from services import lab_notify
            run(root, launcher=Launcher(Path(args.launcher_root)),
                notify=lambda text: lab_notify.send(text, Path(args.launcher_root)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
