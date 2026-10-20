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


def test_job_interrupted_by_shutdown_is_requeued_without_attempt(settings):
    from core.db import open_db
    from worker.runner import Worker

    conn = open_db(settings.database_path)
    w = Worker(settings, handlers={})

    def slow(conn_, settings_, payload, progress):
        w._stop.set()  # як Ctrl+C під час рендеру
        raise RuntimeError("ffmpeg: Immediate exit requested")
    w.handlers = {"render": slow}
    jid = queue.enqueue(conn, "render", {})
    assert w.run_one(conn)
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
    assert row["status"] == "queued" and row["attempts"] == 0 and row["error"] is None


def test_ffmpeg_terminate_all_stops_running(tmp_path):
    import threading
    import time

    from render import ffmpeg

    err = {}

    def run():
        try:
            ffmpeg.run_ffmpeg(["-f", "lavfi", "-i", "testsrc2=d=60:s=320x240", "-f", "null", "-"], duration=60)
        except ffmpeg.FFmpegError as exc:
            err["e"] = exc
    t = threading.Thread(target=run)
    t.start()
    for _ in range(50):
        if ffmpeg._running:
            break
        time.sleep(0.1)
    assert ffmpeg.terminate_all() == 1
    t.join(10)
    assert "e" in err and not ffmpeg._running
