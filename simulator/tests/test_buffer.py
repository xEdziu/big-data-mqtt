from edge_sim.buffer import Outbox


def test_fifo_and_delete(tmp_path):
    box = Outbox(tmp_path / "b.db")
    for i in range(5):
        box.append(f"m{i}")
    rows = box.peek_oldest(3)
    assert [p for _, p in rows] == ["m0", "m1", "m2"]
    box.delete([rows[0][0], rows[1][0]])
    assert box.count() == 3
    assert [p for _, p in box.peek_oldest(10)] == ["m2", "m3", "m4"]


def test_survives_reopen(tmp_path):
    path = tmp_path / "b.db"
    box = Outbox(path)
    box.append("x")
    box.close()
    assert Outbox(path).count() == 1
