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


def test_runner_stops_receiver_once_cools_down_and_always_restarts_it(tmp_path):
    lab_queue.add(tmp_path, "base")
    lab_queue.add(tmp_path, "no-retry")
    launcher, clock, started = FakeLauncher(), Clock(), []

    def spawn(args, log):
        started.append(args)
        return FakeProcess(0 if len(started) == 1 else 3)

    lab_queue.run(tmp_path, launcher=launcher, spawn=spawn, sleep=clock.sleep, now=clock.now, cooldown=300)
    jobs = lab_queue.read(tmp_path)["jobs"]
    assert [j["status"] for j in jobs] == ["done", "failed"]
    assert launcher.commands == ["receiver-stop", "receiver-start"], "приймач зупинено один раз і запущено знову"
    assert started[0][3:6] == ["run", "--name", "q-base"] and "--retry" in started[1]
    assert jobs[1]["started"] - jobs[0]["finished"] >= 300, "пауза між тестами"
    assert lab_queue.read(tmp_path)["runner"]["state"] == "idle"


def test_runner_waits_for_start_time_and_honours_stop(tmp_path):
    lab_queue.add(tmp_path, "base")
    clock = Clock()
    lab_queue.set_start_at(tmp_path, clock.t + 100)
    launcher = FakeLauncher()
    lab_queue.run(tmp_path, launcher=launcher, spawn=lambda a, l: FakeProcess(0), sleep=clock.sleep, now=clock.now)
    job = lab_queue.read(tmp_path)["jobs"][0]
    assert job["started"] >= 1100
    lab_queue.add(tmp_path, "base")
    lab_queue.request_stop(tmp_path)
    launcher = FakeLauncher()
    lab_queue.run(tmp_path, launcher=launcher, spawn=lambda a, l: FakeProcess(0), sleep=clock.sleep, now=clock.now)
    assert lab_queue.read(tmp_path)["jobs"][-1]["status"] == "queued" and launcher.commands == []


def test_receiver_is_restarted_even_when_the_runner_fails(tmp_path):
    lab_queue.add(tmp_path, "base")
    launcher = FakeLauncher()

    def spawn(args, log):
        raise OSError("no python")

    with pytest.raises(OSError):
        lab_queue.run(tmp_path, launcher=launcher, spawn=spawn, sleep=lambda s: None)
    assert launcher.commands == ["receiver-stop", "receiver-start"]


def test_windows_run_only_jobs_that_fit_and_give_the_receiver_back_between_windows(tmp_path):
    for preset in ("base", "no-prompt", "resegment"):
        lab_queue.add(tmp_path, preset)
    clock, launcher, started = Clock(), FakeLauncher(), []
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
    lab_queue.run(tmp_path, launcher=launcher, spawn=spawn, sleep=sleep, now=clock.now, cooldown=300,
                  estimate=lambda preset: minutes[preset] * 60)
    assert [p for p, _ in started] == ["base", "no-prompt", "resegment"]
    assert w1[0] <= started[0][1] < w1[1] and all(w2[0] <= t < w2[1] for _, t in started[1:]), "другий і третій — у другому вікні"
    assert launcher.commands == ["receiver-stop", "receiver-start", "receiver-stop", "receiver-start"], "між вікнами приймач працює"
    assert [j["status"] for j in lab_queue.read(tmp_path)["jobs"]] == ["done", "done", "done"]


def test_estimates_scale_with_records_and_preset(tmp_path):
    base, turbo, reseg = (lab_queue.estimate_seconds(tmp_path, p, 1000) for p in ("base", "turbo", "resegment"))
    assert base == 1000 * lab_queue.SECONDS_PER_RECORD and turbo < base < reseg
