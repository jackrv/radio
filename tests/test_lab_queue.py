import pytest

from services import lab_queue


class FakeLauncher:
    def __init__(self):
        self.stopped, self.commands = False, []

    def receiver_stopped(self):
        return self.stopped

    def send(self, command):
        self.commands.append(command)
        self.stopped = command == "receiver-stop"
        return True


class FakeCapture:
    def __init__(self, *, ready=True):
        self.ready_state, self.events = ready, []
        self.restored_state = True

    def start(self, until):
        self.events.append(("start", until))
        self.restored_state = False

    def renew(self, until):
        self.events.append(("renew", until))

    def ready(self):
        return self.ready_state

    def stop(self):
        self.events.append(("stop",))
        self.restored_state = True

    def restored(self):
        return self.restored_state


class FakeProcess:
    pid = 4242

    def __init__(self, code):
        self.returncode, self.polls = None, 0
        self.code = code

    def poll(self):
        self.polls += 1
        if self.polls > 1:
            self.returncode = self.code
        return self.returncode


class Clock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def test_queue_order_move_cancel_and_validation(tmp_path):
    a = lab_queue.add(tmp_path, "base")
    b = lab_queue.add(tmp_path, "no-prompt", "чи заважає підказка")
    lab_queue.move(tmp_path, b["id"], -1)
    assert [j["preset"] for j in lab_queue.read(tmp_path)["jobs"]] == ["no-prompt", "base"]
    assert lab_queue.cancel(tmp_path, a["id"]) == "removed"
    assert [j["id"] for j in lab_queue.read(tmp_path)["jobs"]] == [b["id"]]
    with pytest.raises(ValueError):
        lab_queue.add(tmp_path, "rm -rf")
    with pytest.raises(ValueError):
        lab_queue.add(tmp_path, "base", "<script>")


def test_prompt_ablation_presets_keep_one_prompt_component_removed(tmp_path):
    phrases = lab_queue.PRESETS["no-whisper-phrases"]["args"]
    dictionary = lab_queue.PRESETS["no-dictionary-prompt"]["args"]
    assert phrases[phrases.index("--prompt") + 1] == "receiver-no-phrases"
    assert dictionary[dictionary.index("--prompt") + 1] == "receiver-no-dictionary"
    assert phrases[phrases.index("--daily-codes") + 1] == "latest"
    assert dictionary[dictionary.index("--daily-codes") + 1] == "latest"


def test_runner_keeps_receiver_listening_and_always_restores_recognition(tmp_path):
    lab_queue.add(tmp_path, "base")
    lab_queue.add(tmp_path, "no-retry")
    launcher, capture, clock, started = FakeLauncher(), FakeCapture(), Clock(), []

    def spawn(args, log):
        started.append(args)
        return FakeProcess(0 if len(started) == 1 else 3)

    lab_queue.run(tmp_path, launcher=launcher, capture=capture, spawn=spawn, sleep=clock.sleep,
                  now=clock.now, cooldown=300)
    jobs = lab_queue.read(tmp_path)["jobs"]
    assert [j["status"] for j in jobs] == ["done", "failed"]
    assert launcher.commands == [], "приймач не зупиняється"
    assert [event[0] for event in capture.events] == ["start", "stop"]
    assert capture.events[0][1] > jobs[0]["started"]
    assert started[0][3:6] == ["run", "--name", "q-base"] and "--retry" in started[1]
    assert jobs[1]["started"] - jobs[0]["finished"] >= 300, "пауза між тестами"
    assert lab_queue.read(tmp_path)["runner"]["state"] == "idle"


def test_runner_waits_for_start_time_and_honours_stop(tmp_path):
    lab_queue.add(tmp_path, "base")
    clock = Clock()
    lab_queue.set_start_at(tmp_path, clock.t + 100)
    launcher = FakeLauncher()
    lab_queue.run(tmp_path, launcher=launcher, capture=FakeCapture(), spawn=lambda a, l: FakeProcess(0),
                  sleep=clock.sleep, now=clock.now)
    job = lab_queue.read(tmp_path)["jobs"][0]
    assert job["started"] >= 1100
    lab_queue.add(tmp_path, "base")
    lab_queue.request_stop(tmp_path)
    launcher = FakeLauncher()
    lab_queue.run(tmp_path, launcher=launcher, capture=FakeCapture(), spawn=lambda a, l: FakeProcess(0),
                  sleep=clock.sleep, now=clock.now)
    assert lab_queue.read(tmp_path)["jobs"][-1]["status"] == "queued" and launcher.commands == []


def test_capture_mode_is_disabled_even_when_the_runner_fails(tmp_path):
    lab_queue.add(tmp_path, "base")
    launcher = FakeLauncher()

    def spawn(args, log):
        raise OSError("no python")

    capture = FakeCapture()
    with pytest.raises(OSError):
        lab_queue.run(tmp_path, launcher=launcher, capture=capture, spawn=spawn, sleep=lambda s: None)
    assert launcher.commands == []
    assert [event[0] for event in capture.events] == ["start", "stop"]


def test_windows_run_only_jobs_that_fit_and_restore_recognition_between_windows(tmp_path):
    for preset in ("base", "no-prompt", "resegment"):
        lab_queue.add(tmp_path, preset)
    clock, launcher, capture, started = Clock(), FakeLauncher(), FakeCapture(), []
    minutes = {"base": 85, "no-prompt": 85, "resegment": 100}

    class Timed(FakeProcess):
        def __init__(self, preset):
            super().__init__(0); self.left = minutes[preset] * 60
        def poll(self):
            if self.left <= 0:
                self.returncode = 0
            return self.returncode

    def spawn(args, log):
        preset = args[args.index("--name") + 1][2:]
        started.append((preset, clock.t))
        process = Timed(preset)
        original = clock.sleep
        def sleep(seconds):
            original(seconds); process.left -= seconds
        clock.sleep_hook = sleep
        return process

    def sleep(seconds):
        hook = getattr(clock, "sleep_hook", None)
        (hook or clock.sleep)(seconds)

    w1, w2 = (clock.t + 600, clock.t + 600 + 115 * 60), (clock.t + 600 + 135 * 60, clock.t + 600 + 355 * 60)
    lab_queue.set_windows(tmp_path, [w1, w2])
    lab_queue.run(tmp_path, launcher=launcher, capture=capture, spawn=spawn, sleep=sleep, now=clock.now, cooldown=300,
                  estimate=lambda preset: minutes[preset] * 60)
    assert [p for p, _ in started] == ["base", "no-prompt", "resegment"]
    assert w1[0] <= started[0][1] < w1[1] and all(w2[0] <= t < w2[1] for _, t in started[1:]), "другий і третій — у другому вікні"
    assert launcher.commands == [], "приймач не зупиняється"
    assert [event[0] for event in capture.events] == ["start", "stop", "start", "stop"]
    assert [j["status"] for j in lab_queue.read(tmp_path)["jobs"]] == ["done", "done", "done"]


def test_runner_does_not_start_whisper_test_until_receiver_confirms_unload(tmp_path):
    lab_queue.add(tmp_path, "base")
    clock, capture, started = Clock(), FakeCapture(ready=False), []
    lab_queue.run(tmp_path, launcher=FakeLauncher(), capture=capture,
                  spawn=lambda args, log: started.append(args), sleep=clock.sleep, now=clock.now)
    job = lab_queue.read(tmp_path)["jobs"][0]
    assert job["status"] == "failed" and "не вивантажив Whisper" in job["error"]
    assert started == []
    assert [event[0] for event in capture.events] == ["start", "stop"]


def test_runner_does_not_enable_capture_when_receiver_is_stopped(tmp_path):
    lab_queue.add(tmp_path, "base")
    launcher, capture = FakeLauncher(), FakeCapture()
    launcher.stopped = True
    lab_queue.run(tmp_path, launcher=launcher, capture=capture,
                  spawn=lambda args, log: pytest.fail("тест не має запускатися"), sleep=lambda _: None)
    job = lab_queue.read(tmp_path)["jobs"][0]
    assert job["status"] == "failed" and "Приймач вимкнений" in job["error"]
    assert capture.events == []


def test_estimates_scale_with_records_and_preset(tmp_path):
    base, turbo, reseg = (lab_queue.estimate_seconds(tmp_path, p, 1000) for p in ("base", "turbo", "resegment"))
    assert base == 1000 * lab_queue.SECONDS_PER_RECORD and turbo < base < reseg


def test_estimate_does_not_apply_turbo_factor_twice(tmp_path):
    import json
    import os
    from datetime import datetime, timezone
    folder = tmp_path / '20261120T010000Z_q-turbo'
    folder.mkdir()
    started = datetime(2026, 11, 20, 1, tzinfo=timezone.utc).timestamp()
    (folder / 'params.json').write_text(json.dumps({'records':100, 'started_utc':'20261120T010000Z'}))
    (folder / 'metrics.json').write_text('{}')
    output = folder / 'results.jsonl'; output.write_text('')
    os.utime(output, (started + 200, started + 200))
    assert lab_queue.estimate_seconds(tmp_path, 'turbo', 100) == 200
    assert lab_queue.estimate_seconds(tmp_path, 'base', 100) == 500


def test_server_plan_matches_executor_and_keeps_cooldown_between_windows():
    jobs = [{'id':str(i),'preset':p,'status':'queued'} for i,p in enumerate(('base','no-prompt','resegment'))]
    estimates = {'base':85*60,'no-prompt':85*60,'resegment':100*60}
    plan = lab_queue.plan_windows([[0,115*60],[135*60,355*60]],jobs,estimates,now=0)
    assert [[p['job']['preset'] for p in slot['jobs']] for slot in plan['plan']] == [['base'],['no-prompt','resegment']]
    assert plan['left'] == []
    short = [{'id':str(i),'preset':'base','status':'queued'} for i in range(2)]
    result = lab_queue.plan_windows([[0,115],[115,230]],short,{'base':100},now=0,cooldown=60)
    assert len(result['plan'][0]['jobs']) == 1 and result['plan'][1]['jobs'] == []
    assert len(result['left']) == 1
    assert lab_queue.first_fitting(short,160,230,lambda _:100) is None


def test_clear_removes_only_done_and_interrupted_jobs_can_be_retried(tmp_path):
    a = lab_queue.add(tmp_path, "base", "еталон")
    b = lab_queue.add(tmp_path, "no-prompt")
    with lab_queue._locked(tmp_path) as state:
        state["jobs"][0]["status"], state["jobs"][1]["status"] = "cancelled", "done"
    lab_queue.clear_finished(tmp_path)
    assert [j["id"] for j in lab_queue.read(tmp_path)["jobs"]] == [a["id"]], "перерваний лишився"
    lab_queue.retry(tmp_path, a["id"])
    jobs = lab_queue.read(tmp_path)["jobs"]
    assert [(j["preset"], j["status"], j["note"]) for j in jobs] == [("base", "queued", "еталон")]
