"""Web Push: «проєкт готовий», «чекає підтвердження вартості».

Ключі VAPID створюються один раз і лежать у library/. На iPhone push працює для PWA,
доданої на головний екран (iOS 16.4+), і лише через HTTPS (Спринт 12: tailscale cert).
"""

from __future__ import annotations

import base64
import json
import logging
import sqlite3
from pathlib import Path

from core.config import Settings

log = logging.getLogger(__name__)


def _keys_dir(settings: Settings) -> Path:
    return settings.library_path


def vapid_private_path(settings: Settings) -> Path:
    return _keys_dir(settings) / "vapid_private.pem"


def public_key(settings: Settings) -> str:
    """Ключ для pushManager.subscribe (applicationServerKey, base64url без «=»)."""
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid01

    path = vapid_private_path(settings)
    if not path.exists():
        v = Vapid01()
        v.generate_keys()
        path.parent.mkdir(parents=True, exist_ok=True)
        v.save_key(str(path))
    v = Vapid01.from_file(str(path))
    raw = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def subscribe(conn: sqlite3.Connection, sub: dict) -> None:
    conn.execute("INSERT INTO push_subscriptions(endpoint, data) VALUES (?, ?)"
                 " ON CONFLICT(endpoint) DO UPDATE SET data = excluded.data",
                 (sub["endpoint"], json.dumps(sub)))


_sender = None  # підміна в тестах


def send(conn: sqlite3.Connection, settings: Settings, title: str, body: str, url: str = "/") -> int:
    """Надсилає на всі підписані пристрої. Повертає, скільки доставлено. Помилки не валять конвеєр."""
    subs = conn.execute("SELECT * FROM push_subscriptions").fetchall()
    if not subs:
        return 0
    payload = json.dumps({"title": title, "body": body, "url": url}, ensure_ascii=False)
    sent = 0
    for s in subs:
        try:
            if _sender is not None:
                _sender(json.loads(s["data"]), payload)
            else:
                from pywebpush import WebPushException, webpush
                try:
                    webpush(json.loads(s["data"]), payload, vapid_private_key=str(vapid_private_path(settings)),
                            vapid_claims={"sub": "mailto:automontage@localhost"}, ttl=86400)
                except WebPushException as exc:
                    if exc.response is not None and exc.response.status_code in (404, 410):
                        conn.execute("DELETE FROM push_subscriptions WHERE id = ?", (s["id"],))  # пристрій відписався
                        continue
                    raise
            sent += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("push не доставлено: %s", exc)
    return sent
