"""Прямий upload з докачуванням за протоколом tus 1.0.0.

Клієнт (tus-js-client у PWA) створює upload через POST, далі шле шматки PATCH-ами;
після обриву зв'язку питає HEAD, з якого байта продовжити.
Розширення: creation, termination.
"""

from __future__ import annotations

import asyncio
import base64
import shutil
import sqlite3
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from core.config import Settings
from core.projects import ProjectError, get_project
from server.deps import db, settings_dep
from sources.ingest import IngestError, ingest_file, media_kind

TUS_VERSION = "1.0.0"
MAX_SIZE = 64 * 1024**3  # 64 ГБ
DISK_RESERVE = 2 * 1024**3  # не займати останні 2 ГБ диска

router = APIRouter(prefix="/api/uploads", tags=["uploads"])
_locks: dict[str, asyncio.Lock] = {}


def _tus_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    h = {"Tus-Resumable": TUS_VERSION, "Cache-Control": "no-store"}
    h.update(extra or {})
    return h


def _require_tus(request: Request) -> None:
    if request.headers.get("Tus-Resumable") != TUS_VERSION:
        raise HTTPException(412, "Потрібен Tus-Resumable: 1.0.0", headers={"Tus-Version": TUS_VERSION})


def parse_metadata(header: str | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for pair in (header or "").split(","):
        pair = pair.strip()
        if not pair:
            continue
        key, _, value = pair.partition(" ")
        try:
            result[key] = base64.b64decode(value).decode("utf-8") if value else ""
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, f"Невалідний Upload-Metadata для «{key}»") from exc
    return result


def uploads_dir(settings: Settings) -> Path:
    p = settings.library_path / ".uploads"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _get_upload(conn: sqlite3.Connection, upload_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM uploads WHERE id = ?", (upload_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Upload не знайдено", headers=_tus_headers())
    return row


@router.options("")
@router.options("/{upload_id}")
def options(upload_id: str | None = None) -> Response:
    return Response(status_code=204, headers=_tus_headers({
        "Tus-Version": TUS_VERSION, "Tus-Extension": "creation,termination",
        "Tus-Max-Size": str(MAX_SIZE)}))


@router.post("")
def create(request: Request, conn: sqlite3.Connection = Depends(db),
           settings: Settings = Depends(settings_dep)) -> Response:
    _require_tus(request)
    try:
        length = int(request.headers["Upload-Length"])
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, "Потрібен Upload-Length") from exc
    if length <= 0 or length > MAX_SIZE:
        raise HTTPException(413, "Файл завеликий або порожній", headers=_tus_headers())
    meta = parse_metadata(request.headers.get("Upload-Metadata"))
    filename = meta.get("filename") or meta.get("name") or ""
    if not filename or media_kind(filename) is None:
        raise HTTPException(415, f"Непідтримуваний формат: {filename or 'без імені'}", headers=_tus_headers())
    try:
        project = get_project(conn, int(meta.get("project_id", "")))
    except (ValueError, ProjectError) as exc:
        raise HTTPException(404, "Проєкт не знайдено", headers=_tus_headers()) from exc

    folder = uploads_dir(settings)
    if shutil.disk_usage(folder).free - length < DISK_RESERVE:
        raise HTTPException(507, "Недостатньо місця на диску ноутбука", headers=_tus_headers())

    upload_id = uuid.uuid4().hex
    part = folder / f"{upload_id}.part"
    part.touch()
    conn.execute("INSERT INTO uploads(id, project_id, filename, length, part_path) VALUES (?, ?, ?, ?, ?)",
                 (upload_id, project["id"], filename, length, str(part)))
    return Response(status_code=201, headers=_tus_headers({
        "Location": f"{request.url.path.rstrip('/')}/{upload_id}", "Upload-Offset": "0"}))


@router.head("/{upload_id}")
def head(upload_id: str, request: Request, conn: sqlite3.Connection = Depends(db)) -> Response:
    _require_tus(request)
    up = _get_upload(conn, upload_id)
    extra = {"Upload-Offset": str(up["offset"]), "Upload-Length": str(up["length"])}
    if up["media_id"]:
        extra["X-Media-Id"] = str(up["media_id"])
    return Response(status_code=200, headers=_tus_headers(extra))


@router.patch("/{upload_id}")
async def patch(upload_id: str, request: Request, conn: sqlite3.Connection = Depends(db),
                settings: Settings = Depends(settings_dep)) -> Response:
    _require_tus(request)
    if request.headers.get("Content-Type") != "application/offset+octet-stream":
        raise HTTPException(415, "Content-Type має бути application/offset+octet-stream")
    lock = _locks.setdefault(upload_id, asyncio.Lock())
    if lock.locked():
        raise HTTPException(423, "Цей upload уже передається", headers=_tus_headers())
    async with lock:
        up = _get_upload(conn, upload_id)
        try:
            client_offset = int(request.headers["Upload-Offset"])
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, "Потрібен Upload-Offset") from exc
        if client_offset != up["offset"]:
            raise HTTPException(409, "Невірний Upload-Offset",
                                headers=_tus_headers({"Upload-Offset": str(up["offset"])}))
        if up["offset"] >= up["length"]:
            headers = {"Upload-Offset": str(up["offset"])}
            # Файл дійшов, але прийом у бібліотеку тоді не вдався — повторюємо.
            media_id = up["media_id"] or await run_in_threadpool(_complete, settings, upload_id)
            headers["X-Media-Id"] = str(media_id)
            return Response(status_code=204, headers=_tus_headers(headers))

        offset = up["offset"]
        part = Path(up["part_path"])
        try:
            with open(part, "r+b") as f:
                f.seek(offset)
                async for chunk in request.stream():
                    if offset + len(chunk) > up["length"]:
                        raise HTTPException(413, "Дані довші за Upload-Length")
                    f.write(chunk)
                    offset += len(chunk)
        finally:
            # Навіть при обриві зберігаємо, скільки дійшло — з цього місця і продовжимо.
            conn.execute("UPDATE uploads SET offset = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
                         " WHERE id = ?", (offset, upload_id))

        headers = {"Upload-Offset": str(offset)}
        if offset == up["length"]:
            media_id = await run_in_threadpool(_complete, settings, upload_id)
            headers["X-Media-Id"] = str(media_id)
        return Response(status_code=204, headers=_tus_headers(headers))


def _complete(settings: Settings, upload_id: str) -> int:
    from core.db import open_db

    conn = open_db(settings.database_path)
    try:
        up = conn.execute("SELECT * FROM uploads WHERE id = ?", (upload_id,)).fetchone()
        project = get_project(conn, up["project_id"])
        try:
            res = ingest_file(conn, settings.library_path, project, Path(up["part_path"]),
                              up["filename"], "upload", move=True)
        except IngestError as exc:
            raise HTTPException(415, str(exc)) from exc
        conn.execute("UPDATE uploads SET media_id = ? WHERE id = ?", (res.media["id"], upload_id))
        return res.media["id"]
    finally:
        conn.close()


@router.delete("/{upload_id}")
def terminate(upload_id: str, request: Request, conn: sqlite3.Connection = Depends(db)) -> Response:
    _require_tus(request)
    up = _get_upload(conn, upload_id)
    if not up["media_id"]:
        Path(up["part_path"]).unlink(missing_ok=True)
    conn.execute("DELETE FROM uploads WHERE id = ?", (upload_id,))
    return Response(status_code=204, headers=_tus_headers())
