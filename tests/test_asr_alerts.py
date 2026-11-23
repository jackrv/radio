import json
import logging

import pytest

from core.recognition_health import RecognitionHealth
from services.asr_alerts import NODE_DOWN_S, Alerts
from services.node_control import NodeControl

LOG = logging.getLogger("test")


class World:
    def __init__(self, tmp_path, **kwargs):
        self.wall, self.mono, self.sent, self.path = 1000.0, 0.0, [], tmp_path / "state.json"
        self.health = RecognitionHealth(clock=lambda: self.mono)
        self.alerts = Alerts(self.sent.append, LOG, self.health, self.path, clock=lambda: self.wall,
                             monotonic=lambda: self.mono, **kwargs)

    def node(self, state, **extra):
        self.path.write_text(json.dumps({"state": state, "at": self.wall, **extra}))

    def go(self, seconds):
        self.wall += seconds
        self.mono += seconds


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_stuck_recognition_alerts_once_and_recovers(world):
    world.health.started()
    for _ in range(3):
        world.go(100)
        world.alerts.tick()
    assert len(world.sent) == 1 and "не відповідає" in world.sent[0]
    world.health.finished(True)
    world.alerts.tick()
    world.alerts.tick()
    assert len(world.sent) == 2 and "знову працює" in world.sent[1]


def test_error_series_alerts(world):
    for _ in range(5):
        world.health.started()
        world.health.finished(False)
    world.alerts.tick()
    assert "5 фразах" in world.sent[0]


def test_unavailable_mac_is_not_a_recognition_failure(world):
    for _ in range(20):
        world.health.started()
        world.health.finished(None)
    world.alerts.tick()
    assert world.sent == []


def test_node_without_input_alerts_after_delay(world):
    for _ in range(int(NODE_DOWN_S // 5) + 2):
        world.node("stopped")
        world.alerts.tick()
        world.go(5)
    assert len(world.sent) == 1 and "вхід не відкрито" in world.sent[0]
    world.node("listening")
    world.alerts.tick()
    assert "знову слухається" in world.sent[1]


def test_dead_node_alerts_but_pause_and_absent_node_do_not(world):
    world.go(500)
    world.alerts.tick()
    assert world.sent == []           # вузла не запускали
    for _ in range(60):
        world.node("paused")
        world.alerts.tick()
        world.go(5)
    assert world.sent == []
    world.go(20)
    world.alerts.tick()
    world.go(NODE_DOWN_S)
    world.alerts.tick()
    assert len(world.sent) == 1 and "не відповідає" in world.sent[0]


def test_short_node_restart_is_silent(world):
    world.node("listening")
    world.alerts.tick()
    world.go(40)
    world.alerts.tick()
    world.node("listening")
    world.alerts.tick()
    assert world.sent == []


def test_gap_is_reported_once_and_old_gap_is_ignored(world):
    world.node("listening", gap={"seconds": 600, "at": 500.0})
    world.alerts.tick()
    assert world.sent == []
    world.go(100)
    for _ in range(3):
        world.node("listening", gap={"seconds": 600, "at": 1050.0})
        world.alerts.tick()
    assert len(world.sent) == 1 and "Розрив приймання" in world.sent[0]


@pytest.mark.parametrize("gap", ["x", {}, {"at": "1", "seconds": 1}, {"at": 5000.0}])
def test_bad_gap_is_ignored(world, gap):
    world.go(1)
    world.node("listening", gap=gap)
    world.alerts.tick()
    assert world.sent == []


def test_node_writes_gap_to_state(tmp_path):
    node = NodeControl(tmp_path, clock=lambda: 77.0)
    node.record_gap(120)
    node.publish()
    assert json.loads((tmp_path / "state.json").read_text())["gap"] == {"seconds": 120.0, "at": 77.0}


def test_mac_offline_alert_is_off_by_default_and_optional(tmp_path):
    silent = World(tmp_path, offline_since=lambda: 0.0)
    silent.go(90000)
    silent.alerts.tick()
    assert silent.sent == []
    since = [0.0]
    loud = World(tmp_path, offline_since=lambda: since[0], offline_alert_s=1800)
    loud.go(1700)
    loud.alerts.tick()
    assert loud.sent == []
    loud.go(200)
    loud.alerts.tick()
    loud.alerts.tick()
    assert len(loud.sent) == 1
    since[0] = None
    loud.alerts.tick()
    assert "знову доступний" in loud.sent[1]


def test_failed_delivery_does_not_stop_checks(tmp_path):
    world = World(tmp_path)
    world.alerts.notify = lambda text: 1 / 0
    world.health.started()
    world.go(200)
    world.alerts.tick()
    assert world.alerts.recognition == "stuck"
