import json
import random
import time

import pytest

from edge_sim.buffer import Outbox
from edge_sim.device import Device
from edge_sim.layout import MineLayout, Route
from edge_sim.profiles import HaulTruck, VentFan


class FakeInfo:
    def __init__(self, ok: bool, raises: bool = False):
        self.ok, self.raises = ok, raises

    def wait_for_publish(self, timeout=None):
        if self.raises:
            raise RuntimeError("no connection")

    def is_published(self):
        return self.ok


class FakeClient:
    """Klient MQTT, któremu można kazać nie potwierdzać wiadomości od n-tej."""

    def __init__(self):
        self.connected = True
        self.fail_from = None  # numer wiadomości, od której brak PUBACK
        self.published = []

    def is_connected(self):
        return self.connected

    def publish(self, topic, payload, qos):
        self.published.append(json.loads(payload))
        n = len(self.published)
        return FakeInfo(ok=self.fail_from is None or n < self.fail_from)


LAYOUT = MineLayout(
    routes={},
    stations={},
    zones=[("strefa", [(100, -10), (200, -10), (200, 10), (100, 10)])],
)


@pytest.fixture
def make_device(tmp_path):
    def make(place=(0.0, 0.0), machine_cls=VentFan, capacity=100):
        rng = random.Random(1)
        client = FakeClient()
        dev = Device("D-01", machine_cls(rng), LAYOUT, place, Outbox(tmp_path / "d.ring", capacity), client, rng)
        return dev, client

    return make


def far_deadline():
    return time.monotonic() + 10


def test_live_record_is_sent_and_removed(make_device):
    dev, client = make_device()
    dev.tick(time.time(), time.monotonic(), 1.0)
    assert dev.flush(far_deadline()) == 1
    assert dev.outbox.count() == 0
    msg = client.published[0]
    assert msg["link"] == "online" and msg["buffered"] == 0 and msg["delay_ms"] >= 0


def test_manual_link_loss_buffers_then_forwards_in_order(make_device):
    dev, client = make_device()
    dev.apply_command("link_loss", {"seconds": 60})
    t0 = time.time()
    for i in range(5):
        dev.tick(t0 - 100 + i, time.monotonic(), 1.0)
        assert dev.flush(far_deadline()) == 0
    assert dev.outbox.count() == 5 and client.published == []

    dev.apply_command("reset", {})
    dev.tick(t0, time.monotonic(), 1.0)
    assert dev.flush(far_deadline()) == 6
    ts = [m["ts"] for m in client.published]
    assert ts == sorted(ts)
    assert [m["buffered"] for m in client.published] == [1] * 5 + [0]
    assert [m["link"] for m in client.published] == ["manual"] * 5 + ["online"]


def test_overflow_during_link_loss_drops_oldest(make_device):
    dev, client = make_device(capacity=3)
    dev.apply_command("link_loss", {"seconds": 60})
    t0 = time.time()
    for i in range(5):
        dev.tick(t0 - 100 + i, time.monotonic(), 1.0)
    dev.apply_command("reset", {})
    dev.tick(t0, time.monotonic(), 1.0)
    assert dev.flush(far_deadline()) == 3
    assert [m["seq"] for m in client.published] == [3, 4, 5]
    assert all(m["dropped"] == 3 for m in client.published)
    assert [m["link"] for m in client.published] == ["manual", "manual", "online"]
    assert client.published[0]["device_id"] == "D-01"


def test_missing_puback_keeps_records(make_device):
    dev, client = make_device()
    for _ in range(4):
        dev.tick(time.time(), time.monotonic(), 1.0)
    client.fail_from = 3  # 3. i 4. wiadomość bez potwierdzenia
    assert dev.flush(far_deadline()) == 2
    assert dev.outbox.count() == 2

    client.fail_from = None
    assert dev.flush(far_deadline()) == 2
    assert dev.outbox.count() == 0


def test_publish_error_keeps_records(make_device, monkeypatch):
    dev, client = make_device()
    dev.tick(time.time(), time.monotonic(), 1.0)
    monkeypatch.setattr(client, "publish", lambda *a, **k: FakeInfo(ok=False, raises=True))
    assert dev.flush(far_deadline()) == 0
    assert dev.outbox.count() == 1


def test_disconnected_broker_buffers(make_device):
    dev, client = make_device()
    client.connected = False
    dev.tick(time.time(), time.monotonic(), 1.0)
    assert dev.flush(far_deadline()) == 0
    assert dev.outbox.count() == 1


def test_zone_on_route_cuts_link(make_device):
    dev, _ = make_device(place=Route([(150, 0), (160, 0)]), machine_cls=HaulTruck)
    dev.tick(time.time(), time.monotonic(), 1.0)
    assert dev.link == "zone"


def test_invalid_command_is_ignored(make_device):
    dev, _ = make_device()

    class Msg:
        payload = b"not json"

    dev._on_message(None, None, Msg())
    dev.apply_command("link_loss", {"seconds": "abc"})
    assert dev.faults.describe() == "none"


def test_route_ping_pong():
    r = Route([(0, 0), (10, 0), (10, 10)])
    assert r.length == 20
    assert r.position_at(5) == (5, 0)
    assert r.position_at(15) == (10, 5)
    assert r.position_at(25) == (10, 5)  # droga powrotna
    assert r.position_at(40) == (0, 0)


def test_every_mobile_machine_eventually_moves():
    from edge_sim.profiles import PROFILES, Faults

    for cls in PROFILES.values():
        if cls.machine_type == "vent_fan":
            continue
        m = cls(random.Random(3))
        speeds = [m.step(1.0, Faults()) and m.move_speed_mps() for _ in range(400)]
        assert max(speeds) > 0, cls.machine_type
