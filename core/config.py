"""Налаштування програми з `.env` і змінних середовища.

Секрети (ключ Gemini, токени Drive) живуть лише тут, ніколи в коді.
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent

# Сервер ніколи не слухає на всіх інтерфейсах.
_FORBIDDEN_HOSTS = {"0.0.0.0", "::", "[::]", ""}


def _check_bind_host(value: str) -> str:
    host = value.strip()
    if host in _FORBIDDEN_HOSTS:
        raise ValueError(f"Адреса «{value}» заборонена: лише 127.0.0.1 або Tailscale (100.64.0.0/10)")
    if host == "localhost":
        return host
    try:
        ip = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError(f"«{value}» — не IP-адреса") from exc
    tailscale_v4 = ipaddress.ip_network("100.64.0.0/10")
    tailscale_v6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
    tailscale_net = tailscale_v4 if ip.version == 4 else tailscale_v6
    if ip.is_loopback or ip in tailscale_net:
        return host
    raise ValueError(f"Адреса «{value}» не локальна і не Tailscale — заборонено")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Шляхи
    library_dir: Path = Path("library")
    inbox_dir: Path = Path("inbox")
    db_path: Path = Path("library/auto-editor.sqlite3")

    # Сервер
    host: str = "127.0.0.1"
    port: int = 8765
    tailscale_host: str = ""
    access_pin: SecretStr = SecretStr("")

    # Gemini і бюджет
    gemini_api_key: SecretStr = SecretStr("")
    gemini_model: str = "gemini-flash-latest"
    gemini_thinking_level: str = "low"   # порожньо — як за замовчуванням у моделі
    gemini_window_seconds: int = Field(180, ge=20)
    gemini_poll_seconds: int = Field(60, ge=5)
    monthly_budget_usd: float = Field(10.0, ge=0)
    confirm_threshold_usd: float = Field(1.0, ge=0)
    daily_request_limit: int = Field(2000, ge=0)

    # Черга
    run_worker_in_server: bool = True
    idle_minutes_before_edit: int = Field(10, ge=1)

    # Стиль, якщо проєкту його ще не призначено (автовибір — Спринт 9)
    default_style: str = "minidv-concert"
    render_height: int = Field(1080, ge=240, le=2160)  # висота готового кліпу (ширина 16:9)

    @property
    def render_size(self) -> tuple[int, int]:
        h = self.render_height - self.render_height % 2
        w = round(h * 16 / 9)
        return w - w % 2, h

    # Аналіз
    default_filter_preset: str = "lofi"
    whisper_enabled: bool = True
    whisper_model: str = "small"
    whisper_language: str = ""  # порожньо — автовизначення

    # Google Drive
    google_client_secret_file: str = ""
    google_token_file: str = ""
    drive_root_folder: str = "Автомонтаж"

    @field_validator("host")
    @classmethod
    def _validate_host(cls, v: str) -> str:
        return _check_bind_host(v)

    @field_validator("tailscale_host")
    @classmethod
    def _validate_tailscale_host(cls, v: str) -> str:
        if not v.strip():
            return ""
        return _check_bind_host(v)

    def resolve(self, path: Path) -> Path:
        """Відносні шляхи — від кореня репозиторію."""
        return path if path.is_absolute() else ROOT_DIR / path

    @property
    def library_path(self) -> Path:
        return self.resolve(self.library_dir)

    @property
    def inbox_path(self) -> Path:
        return self.resolve(self.inbox_dir)

    @property
    def database_path(self) -> Path:
        return self.resolve(self.db_path)

    def ensure_dirs(self) -> None:
        for p in (self.library_path, self.inbox_path, self.database_path.parent):
            p.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
