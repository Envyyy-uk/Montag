import pytest

from core.config import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        library_dir=tmp_path / "library",
        inbox_dir=tmp_path / "inbox",
        db_path=tmp_path / "library" / "test.sqlite3",
        whisper_enabled=False,
        taste_file=tmp_path / "taste.md",
        render_height=240,
    )


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory):
    from tests import media

    d = tmp_path_factory.mktemp("media")
    media.sdr_video(d / "concert.mp4")
    media.sdr_video(d / "vertical.mp4", w=720, h=1280, dur=1)
    media.hdr_video(d / "iphone_hdr.mov")
    media.track(d / "track.mp3")
    return d


@pytest.fixture
def conn(settings):
    from core.db import open_db

    settings.ensure_dirs()
    c = open_db(settings.database_path)
    yield c
    c.close()
