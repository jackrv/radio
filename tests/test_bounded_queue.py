import threading

import pytest

from core.bounded_queue import DropOldestQueue


def test_normal_queue():
    q = DropOldestQueue(3)

    q.put(b"A")
    q.put(b"B")

    assert q.get_nowait() == b"A"
    assert q.get_nowait() == b"B"
    assert q.dropped == 0


def test_drops_oldest():
    q = DropOldestQueue(3)

    for item in (1, 2, 3, 4, 5):
        q.put(item)

    assert [q.get_nowait() for _ in range(3)] == [3, 4, 5]
    assert q.dropped == 2


def test_put_nowait_on_full_queue():
    q = DropOldestQueue(1)

    q.put_nowait("old")
    q.put_nowait("new")

    assert q.get_nowait() == "new"
    assert q.dropped == 1


def test_join_after_dropped_items():
    q = DropOldestQueue(2)

    q.put(1)
    q.put(2)
    q.put(3)

    assert q.get_nowait() == 2
    q.task_done()

    assert q.get_nowait() == 3
    q.task_done()

    q.join()
    assert q.unfinished_tasks == 0


def test_invalid_size():
    with pytest.raises(ValueError):
        DropOldestQueue(0)

    with pytest.raises(ValueError):
        DropOldestQueue(-1)


def test_concurrent_producers():
    q = DropOldestQueue(10)
    threads = []

    def producer(start):
        for i in range(200):
            q.put((start, i))

    for n in range(4):
        thread = threading.Thread(target=producer, args=(n,))
        threads.append(thread)
        thread.start()

    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert q.qsize() == 10
    assert q.dropped == 790
