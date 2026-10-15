from worker import queue


def test_enqueue_dedupe_and_claim(conn):
    a = queue.enqueue(conn, "prepare_media", {"media_id": 1})
    assert queue.enqueue(conn, "prepare_media", {"media_id": 1}) == a
    b = queue.enqueue(conn, "prepare_media", {"media_id": 2})
    queue.enqueue(conn, "unknown_kind", {})
    job = queue.claim(conn, ["prepare_media"])
    assert job["id"] == a and job["status"] == "running" and job["attempts"] == 1
    assert queue.payload(job) == {"media_id": 1}
    assert queue.claim(conn, ["prepare_media"])["id"] == b
    assert queue.claim(conn, ["prepare_media"]) is None  # unknown_kind не береться


def test_fail_retries_then_fails(conn):
    jid = queue.enqueue(conn, "x")
    for _ in range(queue.MAX_ATTEMPTS - 1):
        queue.claim(conn, ["x"])
        assert queue.fail(conn, jid, "boom") == "queued"
    queue.claim(conn, ["x"])
    assert queue.fail(conn, jid, "boom") == "failed"
    assert queue.claim(conn, ["x"]) is None


def test_recover_after_restart(conn):
    jid = queue.enqueue(conn, "x")
    queue.claim(conn, ["x"])
    assert queue.claim(conn, ["x"]) is None
    assert queue.recover(conn) == 1  # «перезапуск ноутбука»
    assert queue.claim(conn, ["x"])["id"] == jid
    queue.finish(conn, jid)
    assert queue.pending_count(conn) == 0
