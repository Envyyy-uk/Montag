"""Google Drive у пам'яті: папки, файли з MD5, що «довантажуються», завантаження і видалення."""

import hashlib
import itertools
from pathlib import Path

from sources.gdrive import FOLDER_MIME, DriveFile


class FakeDrive:
    def __init__(self):
        self.items = {}   # id → dict(name, parent, mime, data)
        self.ids = itertools.count(1)
        self.deleted, self.downloads, self.uploads = [], [], []
        self.corrupt_next = False

    def _new(self, name, parent, mime, data=None):
        fid = f"f{next(self.ids)}"
        self.items[fid] = {"name": name, "parent": parent, "mime": mime, "data": data}
        return fid

    # --- для тестів: «телефон» кладе файли ---
    def put(self, parent, name, data: bytes):
        return self._new(name, parent, "video/mp4", data)

    def mkdir(self, name, parent=None):
        return self._new(name, parent, FOLDER_MIME)

    # --- DriveAPI ---
    def find_folder(self, name, parent):
        return next((i for i, v in self.items.items()
                     if v["name"] == name and v["parent"] == parent and v["mime"] == FOLDER_MIME), None)

    def create_folder(self, name, parent):
        return self.mkdir(name, parent)

    def list_children(self, folder_id):
        out = []
        for i, v in self.items.items():
            if v["parent"] == folder_id:
                d = v["data"]
                out.append(DriveFile(i, v["name"], v["mime"], len(d) if d is not None else None,
                                     hashlib.md5(d).hexdigest() if d is not None else None))
        return out

    def download(self, file_id, dest: Path, on_progress=None):
        data = self.items[file_id]["data"]
        if self.corrupt_next:
            self.corrupt_next = False
            data = data[:-1] + bytes([data[-1] ^ 0xFF])
        Path(dest).write_bytes(data)
        self.downloads.append(file_id)

    def upload(self, path, parent, name, mime):
        self.uploads.append((name, parent))
        return self._new(name, parent, mime, Path(path).read_bytes())

    def delete(self, file_id):
        self.deleted.append(file_id)
        self.items.pop(file_id)

    def names_in(self, folder_id):
        return sorted(v["name"] for v in self.items.values() if v["parent"] == folder_id)
