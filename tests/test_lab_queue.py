import pytest

from shared import lab_queue


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
    b = lab_queue.add(tmp_path, "word-silence", "чи заважає підказка")
    lab_queue.move(tmp_path, b["id"], -1)
    assert [j["preset"] for j in lab_queue.read(tmp_path)["jobs"]] == ["word-silence", "base"]
    assert lab_queue.cancel(tmp_path, a["id"]) == "removed"
    assert [j["id"] for j in lab_queue.read(tmp_path)["jobs"]] == [b["id"]]
    with pytest.raises(ValueError):
        lab_queue.add(tmp_path, "rm -rf")
    with pytest.raises(ValueError):
        lab_queue.add(tmp_path, "base", "<script>")


def test_runner_keeps_receiver_listening_and_always_restores_recognition(tmp_path):
    lab_queue.add(tmp_path, "base")
    lab_queue.add(tmp_path, "dictionary-blocks")
    capture, clock, started = FakeCapture(), Clock(), []

    def spawn(args, log):
        started.append(args)
        return FakeProcess(0 if len(started) == 1 else 3)

    lab_queue.run(tmp_path, capture=capture, spawn=spawn, sleep=clock.sleep,
                  now=clock.now, cooldown=300)
    jobs = lab_queue.read(tmp_path)["jobs"]
    assert [j["status"] for j in jobs] == ["done", "failed"]
    assert [event[0] for event in capture.events] == ["start", "stop"]
    assert capture.events[0][1] > jobs[0]["started"]
    assert started[0][3:6] == ["run", "--name", "q-base"] and "--retry" in started[1]
    assert jobs[1]["started"] - jobs[0]["finished"] >= 300, "пауза між тестами"
    assert lab_queue.read(tmp_path)["runner"]["state"] == "idle"


def test_runner_waits_for_start_time_and_honours_stop(tmp_path):
    lab_queue.add(tmp_path, "base")
    clock = Clock()
    lab_queue.set_start_at(tmp_path, clock.t + 100)
    lab_queue.run(tmp_path, capture=FakeCapture(), spawn=lambda a, l: FakeProcess(0),
                  sleep=clock.sleep, now=clock.now)
    job = lab_queue.read(tmp_path)["jobs"][0]
    assert job["started"] >= 1100
    lab_queue.add(tmp_path, "base")
    lab_queue.request_stop(tmp_path)
    lab_queue.run(tmp_path, capture=FakeCapture(), spawn=lambda a, l: FakeProcess(0),
                  sleep=clock.sleep, now=clock.now)
    assert lab_queue.read(tmp_path)["jobs"][-1]["status"] == "queued"


def test_capture_mode_is_disabled_even_when_the_runner_fails(tmp_path):
    lab_queue.add(tmp_path, "base")

    def spawn(args, log):
        raise OSError("no python")

    capture = FakeCapture()
    with pytest.raises(OSError):
        lab_queue.run(tmp_path, capture=capture, spawn=spawn, sleep=lambda s: None)
    assert [event[0] for event in capture.events] == ["start", "stop"]


def test_window_end_pauses_job_and_next_window_resumes_it_from_the_same_run(tmp_path, monkeypatch):
    for preset in ("base", "word-silence", "resegment"):
        lab_queue.add(tmp_path, preset)
    clock, capture, started = Clock(), FakeCapture(), []
    left = {"base": 85 * 60, "word-silence": 85 * 60, "resegment": 100 * 60}
    current = {}

    class Timed(FakeProcess):
        def __init__(self, preset):
            super().__init__(0); self.preset = preset
        def poll(self):
            if self.returncode is None and left[self.preset] <= 0:
                self.returncode = 0
            return self.returncode
        def wait(self):
            return self.returncode

    def spawn(args, log):
        preset = args[args.index("--name") + 1][2:]
        resume = args[args.index("--resume") + 1] if "--resume" in args else None
        if resume is None:   # як asr_lab: нова тека прогону
            folder = tmp_path / f"{int(clock.t)}_q-{preset}"; folder.mkdir()
        started.append((preset, clock.t, resume))
        current["process"] = Timed(preset)
        return current["process"]

    def sleep(seconds):
        clock.sleep(seconds)
        process = current.get("process")
        if process is not None and process.returncode is None:
            left[process.preset] -= seconds

    def kill(pid):
        current["process"].returncode = -15
    monkeypatch.setattr(lab_queue, "_kill", kill)

    w1, w2 = (clock.t + 600, clock.t + 600 + 115 * 60), (clock.t + 600 + 135 * 60, clock.t + 600 + 355 * 60)
    lab_queue.set_windows(tmp_path, [w1, w2])
    lab_queue.run(tmp_path, capture=capture, spawn=spawn, sleep=sleep, now=clock.now, cooldown=300,
                  estimate=lambda preset: 85 * 60)
    assert [(p, r is not None) for p, _, r in started] == [
        ("base", False), ("word-silence", False), ("word-silence", True), ("resegment", False)]
    assert w1[0] <= started[1][1] < w1[1], "другий тест почався в першому вікні"
    assert all(w2[0] <= t < w2[1] for _, t, _ in started[2:]), "продовження — у другому вікні"
    assert started[2][2].endswith("_q-word-silence"), "та сама тека прогону"
    assert left["word-silence"] <= 0 and left["resegment"] <= 0
    assert [event[0] for event in capture.events if event[0] != "renew"] == ["start", "stop", "start", "stop"]
    assert [j["status"] for j in lab_queue.read(tmp_path)["jobs"]] == ["done", "done", "done"]


def test_manual_pause_stops_queue_and_resume_continues_same_run_first(tmp_path, monkeypatch):
    import json
    a = lab_queue.add(tmp_path, "base"); b = lab_queue.add(tmp_path, "resegment")
    clock, capture, calls = Clock(), FakeCapture(), []
    folder = tmp_path / "20261122T190000Z_q-base"

    class Paused(FakeProcess):
        def poll(self):
            self.polls += 1
            if self.polls == 1:
                lab_queue.pause(tmp_path, a["id"])
            return self.returncode

    def spawn(args, log):
        calls.append(args)
        if "--resume" not in args and "q-base" in args:
            folder.mkdir()
            (folder / "params.json").write_text(json.dumps({"records": 4}))
            (folder / "results.jsonl").write_text('{"id": "1"}\n')
            return Paused(0)
        return FakeProcess(0)

    killed = []
    monkeypatch.setattr(lab_queue, "_kill", killed.append)
    original = Paused.poll
    def poll(self):
        result = original(self)
        if killed and self.returncode is None:
            self.returncode = -15
        return self.returncode
    Paused.poll = poll
    lab_queue.run(tmp_path, capture=capture, spawn=spawn, sleep=clock.sleep, now=clock.now)
    jobs = {j["id"]: j for j in lab_queue.read(tmp_path)["jobs"]}
    assert jobs[a["id"]]["status"] == "paused" and jobs[b["id"]]["status"] == "queued", "черга зупинилась на паузі"
    assert capture.events[-1][0] == "stop", "розпізнавання ефіру відновлено"
    assert lab_queue.resumable(tmp_path, jobs[a["id"]])
    lab_queue.resume(tmp_path, a["id"])
    queued = [j for j in lab_queue.read(tmp_path)["jobs"] if j["status"] == "queued"]
    assert [j["id"] for j in queued] == [a["id"], b["id"]], "продовження — першим"
    assert abs(lab_queue.remaining_fraction(tmp_path, queued[0]) - 0.75) < 1e-9
    lab_queue.run(tmp_path, capture=FakeCapture(), spawn=spawn, sleep=clock.sleep, now=clock.now,
                  cooldown=0)
    assert calls[1][calls[1].index("--resume") + 1] == str(folder)
    assert "--resume" not in calls[2]
    with pytest.raises(LookupError):
        lab_queue.resume(tmp_path, b["id"])


def test_runner_does_not_start_whisper_test_until_receiver_confirms_unload(tmp_path):
    lab_queue.add(tmp_path, "base")
    clock, capture, started = Clock(), FakeCapture(ready=False), []
    lab_queue.run(tmp_path, capture=capture,
                  spawn=lambda args, log: started.append(args), sleep=clock.sleep, now=clock.now)
    job = lab_queue.read(tmp_path)["jobs"][0]
    assert job["status"] == "failed" and "не віддав модель" in job["error"]
    assert started == []
    assert [event[0] for event in capture.events] == ["start", "stop"]


def test_estimates_scale_with_records_and_preset(tmp_path):
    base, reseg = (lab_queue.estimate_seconds(tmp_path, p, 1000) for p in ("base", "resegment"))
    assert base == 1000 * lab_queue.SECONDS_PER_RECORD and base < reseg


def test_estimate_does_not_apply_duration_factor_twice(tmp_path):
    import json
    import os
    from datetime import datetime, timezone
    folder = tmp_path / '20261120T010000Z_q-resegment'
    folder.mkdir()
    started = datetime(2026, 11, 20, 1, tzinfo=timezone.utc).timestamp()
    (folder / 'params.json').write_text(json.dumps({'records':100, 'started_utc':'20261120T010000Z'}))
    (folder / 'metrics.json').write_text('{}')
    output = folder / 'results.jsonl'; output.write_text('')
    os.utime(output, (started + 200, started + 200))
    assert lab_queue.estimate_seconds(tmp_path, 'resegment', 100) == 200
    assert abs(lab_queue.estimate_seconds(tmp_path, 'base', 100) - 200 / 1.2) < 1e-6   # множник знято один раз


def test_server_plan_matches_executor_and_keeps_cooldown_between_windows():
    jobs = [{'id':str(i),'preset':p,'status':'queued'} for i,p in enumerate(('base','word-silence','resegment'))]
    estimates = {'base':85*60,'word-silence':85*60,'resegment':100*60}
    plan = lab_queue.plan_windows([[0,115*60],[135*60,355*60]],jobs,estimates,now=0)
    assert [[p['job']['preset'] for p in slot['jobs']] for slot in plan['plan']] == [['base','word-silence'],['word-silence','resegment']]
    assert [p['partial'] for p in plan['plan'][0]['jobs']] == [False, True]
    assert plan['left'] == []
    split = lab_queue.plan_windows([[0,60*60],[2*3600,5*3600]],jobs[:1],{'base':100*60},now=0)
    first, second = split['plan'][0]['jobs'], split['plan'][1]['jobs']
    assert first[0]['partial'] and not second[0]['partial'], 'не встигає — пауза й продовження в наступному вікні'
    assert first[0]['end'] == 60*60 - lab_queue.WINDOW_END_S
    tiny = lab_queue.plan_windows([[0,5*60]],jobs,estimates,now=0)
    assert tiny['plan'][0]['jobs'] == [] and len(tiny['left']) == 3, 'у 5 хв вікні тест не починається'
    assert not lab_queue.window_usable(0, 5*60) and lab_queue.window_usable(0, 11*60)


def test_clear_removes_only_done_and_interrupted_jobs_can_be_retried(tmp_path):
    a = lab_queue.add(tmp_path, "base", "еталон")
    b = lab_queue.add(tmp_path, "word-silence")
    with lab_queue._locked(tmp_path) as state:
        state["jobs"][0]["status"], state["jobs"][1]["status"] = "cancelled", "done"
    lab_queue.clear_finished(tmp_path)
    assert [j["id"] for j in lab_queue.read(tmp_path)["jobs"]] == [a["id"]], "перерваний лишився"
    lab_queue.retry(tmp_path, a["id"])
    jobs = lab_queue.read(tmp_path)["jobs"]
    assert [(j["preset"], j["status"], j["note"]) for j in jobs] == [("base", "queued", "еталон")]


def test_short_telegram_summary_compares_with_base(tmp_path):
    import json
    from shared import lab_notify
    def run_dir(name, **summary):
        d = tmp_path / name; d.mkdir()
        (d / "metrics.json").write_text(json.dumps({"summary": {"speech_records": 100, **summary}}))
    run_dir("20261122T200000Z_q-base", exact_phrases=10, wer=0.6, digits_exact=0.3, missed_speech=5, false_text_on_non_speech=7)
    run_dir("20261122T210000Z_q-word-silence", exact_phrases=12, wer=0.58, digits_exact=0.31, missed_speech=6, false_text_on_non_speech=3)
    text = lab_notify.summary_text(tmp_path, "Whisper: слова й довгі паузи", "done", "20261122T210000Z_q-word-silence", 42)
    assert "42 хв" in text and "Без правок: 12 (база 10)" in text and "WER: 58,0 % (база 60,0 %)" in text
    assert "вигадки на шумі/сигналах: 3 (база 7)" in text and "Черга завершена" in text
    lab_queue.add(tmp_path, "resegment")
    assert "Далі: «Повторна нарізка запису»" in lab_notify.summary_text(tmp_path, "X", "done", "20261122T210000Z_q-word-silence", 1)
    assert "помилка" in lab_notify.summary_text(tmp_path, "X", "failed", None, 1)
    progress = {"done": 30, "total": 120}
    assert "пауза — готово 30 з 120" in lab_notify.summary_text(tmp_path, "X", "paused", "d", 1, progress=progress)
    assert "наступному вікні" in lab_notify.summary_text(tmp_path, "X", "window_paused", "d", 1, progress=progress)
    assert "можна продовжити" in lab_notify.summary_text(tmp_path, "X", "failed", "d", 1, progress=progress)
    assert lab_notify.start_text("X", 5, 0, resumed=True).startswith("▶️ Лабораторія: продовження «X»")


def test_runner_reports_each_finished_job_through_notify(tmp_path, monkeypatch):
    sent = []
    lab_queue.add(tmp_path, "base")
    capture, clock = FakeCapture(), Clock()

    def spawn(args, log):
        return FakeProcess(0)
    lab_queue.run(tmp_path, spawn=spawn, sleep=clock.sleep, now=clock.now, cooldown=0,
                  capture=capture, notify=sent.append, estimate=lambda preset: 600)
    assert len(sent) == 2 and sent[0].startswith("▶️ Лабораторія: старт «Як зараз у приймачі», орієнтовно 10 хв")
    assert "Як зараз у приймачі" in sent[1] and "Черга завершена" in sent[1]
