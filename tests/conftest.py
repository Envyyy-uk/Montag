import pytest

from core.config import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        library_dir=tmp_path / "library",
        inbox_dir=tmp_path / "inbox",
        db_path=tmp_path / "library" / "test.sqlite3",
    )
