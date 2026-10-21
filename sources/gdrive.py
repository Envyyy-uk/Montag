"""Google Drive як «скринька» для відео поза домом і полиця для готових mp4.

Структура: <DRIVE_ROOT_FOLDER>/<назва проєкту>/  — сюди телефон кладе відео;
           <DRIVE_ROOT_FOLDER>/<назва проєкту>/Готово/  — сюди програма кладе готові mp4.

Забір файлу: помічений щонайменше двічі поспіль (завантаження з телефона завершилось) →
завантаження частинами у .part → перевірка розміру і MD5 з Drive → прийом у бібліотеку →
лише тоді видалення з Drive. Оригінали на ноутбуці ніколи не видаляються.

Вхід: `python -m sources.gdrive auth` — один раз, через браузер на ноутбуці.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from core.config import Settings
from core.projects import ProjectError, create_project, find_by_slug, make_slug
from sources.ingest import IGNORED_EXT, IngestError, ingest_file, media_kind
from worker import queue

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]  # потрібен повний доступ: файли кладе інший застосунок (Drive на iPhone)
FOLDER_MIME = "application/vnd.google-apps.folder"
DONE_FOLDER = "Готово"
CHUNK = 16 * 1024 * 1024


@dataclass
class DriveFile:
    id: str
    name: str
    mime: str
    size: int | None
    md5: str | None
    modified: str = ""

    @property
    def is_folder(self) -> bool:
        return self.mime == FOLDER_MIME


class DriveAPI(Protocol):
    def find_folder(self, name: str, parent: str | None) -> str | None: ...
    def create_folder(self, name: str, parent: str | None) -> str: ...
    def list_children(self, folder_id: str) -> list[DriveFile]: ...
    def download(self, file_id: str, dest: Path, on_progress: Callable[[float], None] | None = None) -> None: ...
    def upload(self, path: Path, parent: str, name: str, mime: str) -> str: ...
    def delete(self, file_id: str) -> None: ...
    def trash(self, file_id: str) -> None: ...


def _q(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


class GoogleDrive:
    def __init__(self, creds):
        from googleapiclient.discovery import build

        self.svc = build("drive", "v3", credentials=creds, cache_discovery=False)

    def find_folder(self, name: str, parent: str | None) -> str | None:
        q = f"name = '{_q(name)}' and mimeType = '{FOLDER_MIME}' and trashed = false"
        q += f" and '{parent}' in parents" if parent else " and 'root' in parents"
        res = self.svc.files().list(q=q, fields="files(id)", pageSize=1).execute()
        files = res.get("files", [])
        return files[0]["id"] if files else None

    def create_folder(self, name: str, parent: str | None) -> str:
        body = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent] if parent else ["root"]}
        return self.svc.files().create(body=body, fields="id").execute()["id"]

    def list_children(self, folder_id: str) -> list[DriveFile]:
        out, token = [], None
        while True:
            res = self.svc.files().list(
                q=f"'{folder_id}' in parents and trashed = false", pageSize=1000, pageToken=token,
                fields="nextPageToken, files(id, name, mimeType, size, md5Checksum, modifiedTime)").execute()
            for f in res.get("files", []):
                out.append(DriveFile(f["id"], f["name"], f["mimeType"], int(f["size"]) if "size" in f else None,
                                     f.get("md5Checksum"), f.get("modifiedTime", "")))
            token = res.get("nextPageToken")
            if not token:
                return out

    def download(self, file_id: str, dest: Path, on_progress=None) -> None:
        from googleapiclient.http import MediaIoBaseDownload

        req = self.svc.files().get_media(fileId=file_id)
        with open(dest, "wb") as fh:
            dl = MediaIoBaseDownload(fh, req, chunksize=CHUNK)
            done = False
            while not done:
                status, done = dl.next_chunk(num_retries=5)
                if on_progress and status:
                    on_progress(status.progress())

    def upload(self, path: Path, parent: str, name: str, mime: str) -> str:
        from googleapiclient.http import MediaFileUpload

        media = MediaFileUpload(str(path), mimetype=mime, resumable=True, chunksize=CHUNK)
        req = self.svc.files().create(body={"name": name, "parents": [parent]}, media_body=media, fields="id")
        resp = None
        while resp is None:
            _status, resp = req.next_chunk(num_retries=5)
        return resp["id"]

    def delete(self, file_id: str) -> None:
        self.svc.files().delete(fileId=file_id).execute()

    def trash(self, file_id: str) -> None:
        """У кошик Drive (30 днів можна відновити)."""
        self.svc.files().update(fileId=file_id, body={"trashed": True}).execute()


# ---------- вхід ----------

def load_credentials(settings: Settings):
    """Збережений токен (оновлюється сам). None — Drive не налаштовано."""
    if not settings.google_token_file:
        return None
    path = settings.resolve(Path(settings.google_token_file))
    if not path.exists():
        return None
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        path.write_text(creds.to_json(), encoding="utf-8")
    return creds if creds.valid else None


def authorize(settings: Settings) -> Path:
    from google_auth_oauthlib.flow import InstalledAppFlow

    secret = settings.resolve(Path(settings.google_client_secret_file))
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    token = settings.resolve(Path(settings.google_token_file or "library/google_token.json"))
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(creds.to_json(), encoding="utf-8")
    return token


_override: DriveAPI | None = None  # для тестів


def configured(settings: Settings) -> bool:
    if _override is not None:
        return True
    return bool(settings.google_token_file) and settings.resolve(Path(settings.google_token_file)).exists()


def get_drive(settings: Settings) -> DriveAPI | None:
    if _override is not None:
        return _override
    creds = load_credentials(settings)
    return GoogleDrive(creds) if creds else None


# ---------- стан у базі ----------

def _state(conn: sqlite3.Connection, key: str) -> dict:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else {}


def _save_state(conn: sqlite3.Connection, key: str, value: dict) -> None:
    conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, json.dumps(value, ensure_ascii=False)))


def root_folder(conn: sqlite3.Connection, settings: Settings, drive: DriveAPI) -> str:
    st = _state(conn, "drive_root")
    if st.get("name") == settings.drive_root_folder and st.get("id"):
        return st["id"]
    fid = drive.find_folder(settings.drive_root_folder, None) or drive.create_folder(settings.drive_root_folder, None)
    _save_state(conn, "drive_root", {"name": settings.drive_root_folder, "id": fid})
    return fid


def project_folder(conn: sqlite3.Connection, settings: Settings, drive: DriveAPI, project: sqlite3.Row) -> str:
    folders = _state(conn, "drive_projects")
    key = str(project["id"])
    if key in folders:
        return folders[key]["id"]
    root = root_folder(conn, settings, drive)
    fid = drive.find_folder(project["name"], root) or drive.create_folder(project["name"], root)
    folders[key] = {"id": fid, "name": project["name"]}
    _save_state(conn, "drive_projects", folders)
    return fid


def done_folder(conn: sqlite3.Connection, settings: Settings, drive: DriveAPI, project: sqlite3.Row) -> str:
    key = str(project["id"])
    pf = project_folder(conn, settings, drive, project)
    folders = _state(conn, "drive_projects")
    if folders[key].get("done"):
        return folders[key]["done"]
    did = drive.find_folder(DONE_FOLDER, pf) or drive.create_folder(DONE_FOLDER, pf)
    folders[key]["done"] = did
    _save_state(conn, "drive_projects", folders)
    return did


# ---------- етапи ----------

def md5_file(path: Path) -> str:
    h = hashlib.md5()  # noqa: S324 — порівняння з md5Checksum, який віддає Drive
    with open(path, "rb") as f:
        while block := f.read(4 * 1024 * 1024):
            h.update(block)
    return h.hexdigest()


def drive_create_folder(conn: sqlite3.Connection, settings: Settings, payload: dict, progress) -> None:
    drive = get_drive(settings)
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (payload["project_id"],)).fetchone()
    if drive is None or project is None:
        return
    done_folder(conn, settings, drive, project)


class DrivePoller:
    """Опитування Drive у воркері: нові папки → проєкти, нові файли → завдання на забір."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._seen: dict[str, tuple[int | None, str | None]] = {}
        self._music_seen: dict = {}

    def _poll_music(self, conn, drive, root: str) -> None:
        from sources import music
        try:
            music.poll_drive(conn, self.settings, drive, music_folder(conn, self.settings, drive, root), self._music_seen)
        except Exception as exc:  # noqa: BLE001 — музика не має ламати прийом відео
            log.warning("Drive «%s»: %s", self.settings.drive_music_folder, exc)

    def poll(self, conn: sqlite3.Connection) -> list[str]:
        drive = get_drive(self.settings)
        if drive is None:
            return []
        root = root_folder(conn, self.settings, drive)
        self._poll_music(conn, drive, root)
        folders = _state(conn, "drive_projects")
        # проєкти, створені до підключення Drive, теж отримують папку
        for row in conn.execute("SELECT id FROM projects").fetchall():
            if str(row["id"]) not in folders:
                queue.enqueue(conn, "drive_create_folder", {"project_id": row["id"]}, project_id=row["id"])
        by_folder = {v["id"]: int(k) for k, v in folders.items()}
        queued, alive = [], set()
        for f in drive.list_children(root):
            if not f.is_folder or f.name == self.settings.drive_music_folder:
                continue  # «Музика» — бібліотека треків, а не проєкт
            pid = by_folder.get(f.id)
            if pid is None:  # папку створили з телефона — новий проєкт (або вже наявний з такою назвою)
                try:
                    row = find_by_slug(conn, make_slug(f.name)) or create_project(conn, self.settings.library_path, f.name)
                except ProjectError:
                    continue
                pid = row["id"]
                folders[str(pid)] = {"id": f.id, "name": f.name}
                _save_state(conn, "drive_projects", folders)
                by_folder[f.id] = pid
            for item in drive.list_children(f.id):
                if item.is_folder or item.name.startswith(".") or media_kind(item.name) is None \
                        or Path(item.name).suffix.lower() in IGNORED_EXT:
                    continue
                alive.add(item.id)
                sig = (item.size, item.md5)
                prev = self._seen.get(item.id)
                self._seen[item.id] = sig
                if prev != sig or item.size is None:
                    continue  # ще завантажується
                queue.enqueue(conn, "drive_fetch", {"file_id": item.id, "name": item.name, "size": item.size,
                                                    "md5": item.md5, "project_id": pid}, project_id=pid)
                queued.append(item.id)
        for gone in set(self._seen) - alive:
            self._seen.pop(gone, None)
        return queued


def music_folder(conn: sqlite3.Connection, settings: Settings, drive: DriveAPI, root: str) -> str:
    st = _state(conn, "drive_music_folder")
    if st.get("name") == settings.drive_music_folder and st.get("id"):
        return st["id"]
    fid = drive.find_folder(settings.drive_music_folder, root) or drive.create_folder(settings.drive_music_folder, root)
    _save_state(conn, "drive_music_folder", {"name": settings.drive_music_folder, "id": fid})
    return fid


def drive_trash(conn: sqlite3.Connection, settings: Settings, payload: dict, progress) -> None:
    drive = get_drive(settings)
    if drive is None:
        return
    drive.trash(payload["file_id"])


def drive_fetch(conn: sqlite3.Connection, settings: Settings, payload: dict, progress) -> None:
    drive = get_drive(settings)
    if drive is None:
        raise RuntimeError("Google Drive не підключено")
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (payload["project_id"],)).fetchone()
    if project is None:
        return
    tmp_dir = settings.library_path / ".uploads"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    part = tmp_dir / f"drive_{payload['file_id']}.part"
    drive.download(payload["file_id"], part, lambda p: progress(0.9 * p))
    size = part.stat().st_size
    if payload.get("size") is not None and size != payload["size"]:
        part.unlink(missing_ok=True)
        raise IngestError(f"розмір не збігся: {size} ≠ {payload['size']}")
    if payload.get("md5") and md5_file(part) != payload["md5"]:
        part.unlink(missing_ok=True)
        raise IngestError("MD5 не збігся з Drive — файл пошкоджено під час завантаження")
    res = ingest_file(conn, settings.library_path, project, part, payload["name"], "drive", move=True)
    log.info("drive: %s → медіа %s (%s)", payload["name"], res.media["id"], "нове" if res.created else "дублікат")
    drive.delete(payload["file_id"])  # лише після успішного прийому
    progress(1.0)


def drive_upload(conn: sqlite3.Connection, settings: Settings, payload: dict, progress) -> None:
    """Готові mp4 → <проєкт>/Готово/. Кліп — 720p-превʼю, вертикалі — як є."""
    drive = get_drive(settings)
    if drive is None:
        return
    edit = conn.execute("SELECT * FROM edits WHERE id = ?", (payload["edit_id"],)).fetchone()
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (edit["project_id"],)).fetchone()
    if edit["kind"] == "clip":
        files = [edit["preview_path"]]
    else:
        files = [r["path"] for r in json.loads(edit["summary"] or "{}").get("reels", [])]
    uploaded = _state(conn, "drive_uploaded")
    parent = done_folder(conn, settings, drive, project)
    for i, rel in enumerate(files):
        if not rel or rel in uploaded:
            continue
        path = settings.library_path / rel
        uploaded[rel] = drive.upload(path, parent, path.name, "video/mp4")
        _save_state(conn, "drive_uploaded", uploaded)
        progress((i + 1) / len(files))


def status(conn: sqlite3.Connection, settings: Settings) -> dict:
    return {"configured": configured(settings),
            "root": settings.drive_root_folder, "projects": _state(conn, "drive_projects"),
            "uploaded": len(_state(conn, "drive_uploaded"))}


if __name__ == "__main__":
    import sys

    from core.config import get_settings

    if sys.argv[1:] == ["auth"]:
        s = get_settings()
        if not s.google_client_secret_file:
            raise SystemExit("Вкажи GOOGLE_CLIENT_SECRET_FILE у .env (файл client_secret.json з Google Cloud)")
        print(f"Токен збережено: {authorize(s)}")
    else:
        raise SystemExit("Використання: python -m sources.gdrive auth")
