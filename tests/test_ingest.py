import os
import shutil
import stat
from datetime import datetime, timedelta, timezone

import pytest

from core.projects import ProjectError, create_project, make_slug
from sources.inbox import InboxScanner
from sources.ingest import IngestError, ingest_file, safe_filename
from worker import queue
from worker.stages import auto_queue_idle


def test_slug():
    assert make_slug("  Концерт   Brixton ") == "Концерт Brixton"
    assert make_slug('a/b:c*?') == "abc"
    assert make_slug("CON") == "_CON"
    with pytest.raises(ProjectError):
        make_slug(" ?? ")


def test_duplicate_project(conn, settings):
    create_project(conn, settings.library_path, "Концерт Brixton")
    with pytest.raises(ProjectError):
        create_project(conn, settings.library_path, "концерт brixton")


def test_safe_filename():
    assert safe_filename("..\\..\\evil.mp4") == "evil.mp4"
    assert safe_filename("../x/IMG_0001.MOV") == "IMG_0001.MOV"


def test_ingest_copy_dedupe_and_readonly(conn, settings, media_dir, tmp_path):
    p = create_project(conn, settings.library_path, "Тест")
    src = tmp_path / "IMG_1.mp4"
    shutil.copy(media_dir / "concert.mp4", src)
    r1 = ingest_file(conn, settings.library_path, p, src, "IMG_1.mp4", "upload", move=False)
    assert r1.created and src.exists()
    dst = settings.library_path / r1.media["original_path"]
    assert dst.read_bytes() == src.read_bytes()
    assert not os.stat(dst).st_mode & stat.S_IWUSR  # оригінал лише для читання
    assert r1.media["kind"] == "video"
    # той самий вміст — не дублюється, upload-частина прибирається
    r2 = ingest_file(conn, settings.library_path, p, src, "other.mp4", "upload", move=True)
    assert not r2.created and r2.media["id"] == r1.media["id"] and not src.exists()
    # інший файл з тим самим ім'ям — не перезаписує
    other = tmp_path / "IMG_1.mp4"
    shutil.copy(media_dir / "vertical.mp4", other)
    r3 = ingest_file(conn, settings.library_path, p, other, "IMG_1.mp4", "upload", move=True)
    assert r3.media["original_path"].endswith("IMG_1 (2).mp4")
    jobs = conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='prepare_media'").fetchone()[0]
    assert jobs == 2


def test_ingest_rejects_unknown(conn, settings, tmp_path):
    p = create_project(conn, settings.library_path, "Тест")
    f = tmp_path / "notes.txt"
    f.write_text("x")
    with pytest.raises(IngestError):
        ingest_file(conn, settings.library_path, p, f, f.name, "upload", move=False)


def test_inbox_waits_until_stable(conn, settings, media_dir):
    card = settings.inbox_path / "Поїздка Карпати" / "DCIM" / "100GOPRO"
    card.mkdir(parents=True)
    shutil.copy(media_dir / "concert.mp4", card / "GX010001.MP4")
    (card / "GX010001.LRV").write_bytes(b"low-res")
    (card / "GX010001.THM").write_bytes(b"thumb")
    scanner = InboxScanner(settings.inbox_path, settings.library_path)
    future = datetime.now().timestamp() + 60
    assert scanner.scan(conn, now=future) == []  # перше сканування лише запам'ятовує розмір
    created = scanner.scan(conn, now=future)
    assert len(created) == 1
    assert not (card / "GX010001.MP4").exists()
    assert (card / "GX010001.LRV").exists()  # службові файли не чіпаємо
    project = conn.execute("SELECT * FROM projects").fetchone()
    assert project["name"] == "Поїздка Карпати"


def test_inbox_growing_file_not_taken(conn, settings, media_dir):
    folder = settings.inbox_path / "Тест"
    folder.mkdir(parents=True)
    f = folder / "a.mp4"
    f.write_bytes(b"x" * 10)
    scanner = InboxScanner(settings.inbox_path, settings.library_path)
    future = datetime.now().timestamp() + 60
    scanner.scan(conn, now=future)
    with open(f, "ab") as h:
        h.write(b"y" * 10)  # копіювання з карти триває
    assert scanner.scan(conn, now=future) == []


def test_auto_queue_idle(conn, settings, media_dir, tmp_path):
    p = create_project(conn, settings.library_path, "Тест")
    src = tmp_path / "a.mp4"
    shutil.copy(media_dir / "concert.mp4", src)
    ingest_file(conn, settings.library_path, p, src, "a.mp4", "upload", move=True)
    later = datetime.now(timezone.utc) + timedelta(minutes=11)
    assert auto_queue_idle(conn, 10, later) == []  # проксі ще готуються
    job = queue.claim(conn, ["prepare_media"])
    queue.finish(conn, job["id"])
    assert auto_queue_idle(conn, 10, datetime.now(timezone.utc)) == []  # ще не минуло 10 хв
    assert auto_queue_idle(conn, 10, later) == [p["id"]]
    assert conn.execute("SELECT status FROM projects").fetchone()[0] == "queued"
    assert auto_queue_idle(conn, 10, later) == []  # двічі не ставить
