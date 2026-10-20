import random

import pytest

from edge_sim.buffer import MAX_PAYLOAD, SLOT_HEADER, SLOT_SIZE, Outbox
from edge_sim.profiles import PROFILES
from edge_sim.record import RecordCodec


def payloads(box, limit=100):
    return [p for _, p in box.peek_oldest(limit)]


def test_fifo_and_ack(tmp_path):
    box = Outbox(tmp_path / "b.ring", 10)
    for i in range(5):
        box.append(b"m%d" % i)
    rows = box.peek_oldest(3)
    assert [p for _, p in rows] == [b"m0", b"m1", b"m2"]
    box.ack_through(rows[1][0])
    assert box.count() == 3
    assert payloads(box) == [b"m2", b"m3", b"m4"]


def test_survives_reopen_and_keeps_seq(tmp_path):
    path = tmp_path / "b.ring"
    box = Outbox(path, 10)
    for i in range(3):
        box.append(b"x%d" % i)
    box.ack_through(box.peek_oldest(1)[0][0])
    box.close()

    box = Outbox(path, 10)
    assert [s for s, _ in box.peek_oldest(10)] == [1, 2]
    box.ack_through(2)
    box.close()

    box = Outbox(path, 10)  # wszystko wysłane, a numeracja i tak jest ciągła
    box.append(b"next")
    assert box.peek_oldest(1)[0][0] == 3


def test_overflow_drops_oldest_and_persists_counter(tmp_path):
    path = tmp_path / "b.ring"
    box = Outbox(path, 4)
    for i in range(10):
        box.append(b"m%d" % i)
    assert box.count() == 4 and box.dropped == 6
    assert payloads(box) == [b"m6", b"m7", b"m8", b"m9"]
    box.close()

    box = Outbox(path, 4)
    assert box.dropped == 6
    assert payloads(box) == [b"m6", b"m7", b"m8", b"m9"]


def test_torn_write_is_skipped(tmp_path):
    path = tmp_path / "b.ring"
    box = Outbox(path, 4)
    for i in range(3):
        box.append(b"m%d" % i)
    box.close()
    with open(path, "r+b") as f:  # zanik zasilania w trakcie zapisu ostatniego slotu
        f.seek(SLOT_SIZE * 3 + SLOT_HEADER.size)
        f.write(b"\x00\x00")
    assert payloads(Outbox(path, 4)) == [b"m0", b"m1"]


def test_capacity_change_keeps_newest(tmp_path):
    path = tmp_path / "b.ring"
    box = Outbox(path, 8)
    for i in range(6):
        box.append(b"m%d" % i)
    box.close()

    box = Outbox(path, 3)
    assert payloads(box) == [b"m3", b"m4", b"m5"] and box.dropped == 3
    box.append(b"m6")
    assert [s for s, _ in box.peek_oldest(10)] == [4, 5, 6]


def test_payload_too_big(tmp_path):
    with pytest.raises(ValueError):
        Outbox(tmp_path / "b.ring", 4).append(b"x" * (MAX_PAYLOAD + 1))


@pytest.mark.parametrize("cls", PROFILES.values(), ids=lambda c: c.machine_type)
def test_every_profile_roundtrips_through_slot(cls):
    from edge_sim.profiles import Faults

    codec = RecordCodec(cls.record_fields())
    assert codec.size <= MAX_PAYLOAD
    fields = cls(random.Random(5)).step(1.0, Faults(overheat=True))
    assert list(fields) == list(cls.record_fields())
    record = {"ts": 1_760_000_000_123, "link": "zone", "faults": "overheat,stopped",
              "x": 123.4, "y": -56.7, **fields, "buffer_size": 42}
    assert codec.decode(codec.encode(record)) == record
