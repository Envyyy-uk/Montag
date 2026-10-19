"""Хто підключений до MCP з iPhone (Claude) і відкликання доступу."""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from core.config import Settings
from server.deps import db, settings_dep

router = APIRouter(prefix="/api/mcp", tags=["mcp"])


@router.get("")
def status(conn: sqlite3.Connection = Depends(db), settings: Settings = Depends(settings_dep)) -> dict:
    url = None
    if settings.mcp_remote:
        from mcp_server.remote import public_url
        url = public_url(settings)
    rows = conn.execute(
        "SELECT c.*, (SELECT COUNT(*) FROM mcp_tokens t WHERE t.client_id = c.client_id"
        " AND t.expires_at > CAST(strftime('%s', 'now') AS INTEGER)) AS tokens"
        " FROM mcp_clients c WHERE c.authorized_at IS NOT NULL ORDER BY c.authorized_at DESC").fetchall()
    return {"enabled": settings.mcp_remote, "url": f"{url}/mcp" if url else None,
            "clients": [{"client_id": r["client_id"], "name": json.loads(r["data"]).get("client_name") or "MCP-клієнт",
                         "authorized_at": r["authorized_at"], "last_used_at": r["last_used_at"],
                         "active": r["tokens"] > 0} for r in rows]}


@router.delete("/clients/{client_id}")
def revoke(client_id: str, conn: sqlite3.Connection = Depends(db)) -> dict:
    """Відкликає доступ: усі токени клієнта стають недійсними одразу."""
    conn.execute("DELETE FROM mcp_tokens WHERE client_id = ?", (client_id,))
    if not conn.execute("DELETE FROM mcp_clients WHERE client_id = ?", (client_id,)).rowcount:
        raise HTTPException(404, "Клієнта не знайдено")
    return {"revoked": client_id}
