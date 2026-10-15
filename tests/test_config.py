import pytest
from pydantic import ValidationError

from core.config import Settings


def make(**kw):
    return Settings(_env_file=None, **kw)


def test_defaults():
    s = make()
    assert s.host == "127.0.0.1"
    assert s.monthly_budget_usd == 10
    assert s.confirm_threshold_usd == 1
    assert s.idle_minutes_before_edit == 10


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "", "192.168.1.10", "8.8.8.8", "example.com"])
def test_public_hosts_rejected(host):
    with pytest.raises(ValidationError):
        make(host=host)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "100.101.102.103"])
def test_local_and_tailscale_allowed(host):
    assert make(host=host).host == host


def test_tailscale_host_validated():
    assert make(tailscale_host="").tailscale_host == ""
    assert make(tailscale_host="100.64.0.5").tailscale_host == "100.64.0.5"
    with pytest.raises(ValidationError):
        make(tailscale_host="0.0.0.0")


def test_secret_not_in_repr():
    s = make(gemini_api_key="super-secret-key")
    assert "super-secret-key" not in repr(s)
    assert s.gemini_api_key.get_secret_value() == "super-secret-key"


def test_negative_budget_rejected():
    with pytest.raises(ValidationError):
        make(monthly_budget_usd=-1)


def test_ensure_dirs(settings):
    settings.ensure_dirs()
    assert settings.library_path.is_dir()
    assert settings.inbox_path.is_dir()


def test_cli_init(tmp_path, monkeypatch, capsys):
    import core.__main__ as cli

    monkeypatch.setattr(cli, "get_settings", lambda: Settings(
        _env_file=None, library_dir=tmp_path / "lib", inbox_dir=tmp_path / "in",
        db_path=tmp_path / "lib" / "db.sqlite3"))
    assert cli.main(["init"]) == 0
    assert (tmp_path / "lib" / "db.sqlite3").exists()
    assert "схема v1" in capsys.readouterr().out
