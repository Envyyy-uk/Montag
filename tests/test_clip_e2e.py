"""Повний прогін Режиму 1: 3 «концертні» відео + трек → кліп 60 с."""

import json
import subprocess

import numpy as np
import pytest
from fastapi.testclient import TestClient

from core.db import open_db
from core.projects import create_project
from server.app import create_app
from sources.ingest import ingest_file
from worker.runner import Worker
from worker.stages import request_edit


def _probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-print_format", "json", "-show_streams",
                          str(path)], capture_output=True, text=True, check=True).stdout
    return {s["codec_type"]: s for s in json.loads(out)["streams"]}


class FakeFaceDetector:
    """Одне обличчя, що повільно пливе кадром, — щоб перевірити кадрування без справжніх облич."""

    def __init__(self):
        self.n = 0

    def detect(self, rgb):
        from analysis.framing import Box
        self.n += 1
        cx = 0.4 + 0.1 * np.sin(self.n / 20)
        return [Box(cx - 0.04, 0.3, cx + 0.04, 0.45, 0.9, "face")]

    def close(self):
        pass


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    from analysis import framing
    from core.config import Settings
    from tests import media

    mp = pytest.MonkeyPatch()
    mp.setattr(framing, "make_detector", lambda models_dir: FakeFaceDetector())

    root = tmp_path_factory.mktemp("e2e")
    settings = Settings(_env_file=None, library_dir=root / "library", inbox_dir=root / "inbox",
                        db_path=root / "library" / "db.sqlite3", whisper_enabled=False,
                        render_height=360, taste_file=root / "taste.md")
    settings.ensure_dirs()
    conn = open_db(settings.database_path)
    p = create_project(conn, settings.library_path, "Концерт Brixton")
    for i in range(3):
        src = media.analysis_video(root / f"cam{i}.mp4")
        ingest_file(conn, settings.library_path, p, src, f"IMG_{i}.mp4", "upload", move=True)
    ingest_file(conn, settings.library_path, p, media.song(root / "track.mp3"), "track.mp3", "upload", move=True)
    w = Worker(settings)
    while w.run_one(conn):
        pass
    request_edit(conn, p["id"])  # без ключа Gemini — локальні оцінки
    while w.run_one(conn):
        pass
    mp.undo()
    return settings, conn, p


def test_clip_built(built):
    settings, conn, p = built
    jobs = {(j["kind"], j["status"]) for j in conn.execute("SELECT * FROM jobs")}
    assert ("edit_project", "done") in jobs, list(conn.execute("SELECT kind, status, error FROM jobs"))
    edit = conn.execute("SELECT * FROM edits").fetchone()
    tl = json.loads((settings.library_path / edit["timeline_path"]).read_text(encoding="utf-8"))
    assert 58 <= tl["duration"] <= 62
    assert tl["fps"] == 30.0
    assert conn.execute("SELECT status FROM projects").fetchone()[0] == "edited"


def test_all_cuts_on_beats(built):
    settings, conn, p = built
    from analysis.track import TrackAnalysis

    edit = conn.execute("SELECT * FROM edits").fetchone()
    tl = json.loads((settings.library_path / edit["timeline_path"]).read_text(encoding="utf-8"))
    track_id = tl["track_media_id"]
    ta = TrackAnalysis.load(settings.library_path / p["slug"] / "analysis" / f"{track_id}_track.json")
    beats = np.array(ta.beats) - tl["track_in"]
    for c in tl["clips"][1:]:
        assert np.abs(beats - c["start"]).min() <= 0.5 / tl["fps"] + 1e-6, c["start"]


def test_no_rejected_footage(built):
    settings, conn, p = built
    edit = conn.execute("SELECT * FROM edits").fetchone()
    tl = json.loads((settings.library_path / edit["timeline_path"]).read_text(encoding="utf-8"))
    rejected = conn.execute("SELECT media_id, start, end FROM segments WHERE reject_reason IS NOT NULL").fetchall()
    for c in tl["clips"]:
        dur = c["end"] - c["start"]
        mid = c["src_in"] + dur / 2
        for r in rejected:
            if r["media_id"] == c["media_id"]:
                assert not (r["start"] < mid < r["end"]), (c, dict(r))


def test_live_inserts(built):
    settings, conn, p = built
    edit = conn.execute("SELECT * FROM edits").fetchone()
    tl = json.loads((settings.library_path / edit["timeline_path"]).read_text(encoding="utf-8"))
    ins = tl["inserts"]
    assert len(ins) >= 2
    for x, y in zip(ins, ins[1:]):
        assert y["start"] - x["end"] >= 8 - 1e-6
    assert {m["kind"] for m in tl["markers"]} >= {"insert"}
    kinds = [c["kind"] for c in tl["clips"]]
    assert kinds.count("insert") == len(ins)


def test_export_preview_and_mix(built):
    settings, conn, p = built
    edit = conn.execute("SELECT * FROM edits").fetchone()
    tl = json.loads((settings.library_path / edit["timeline_path"]).read_text(encoding="utf-8"))
    final = _probe(settings.library_path / edit["export_path"])
    assert (final["video"]["width"], final["video"]["height"]) == settings.render_size
    assert int(final["video"]["nb_read_frames"]) == round(tl["duration"] * tl["fps"])
    assert abs(float(final["audio"]["duration"]) - tl["duration"]) < 0.1
    prev = _probe(settings.library_path / edit["preview_path"])
    assert int(prev["video"]["height"]) == 360  # превʼю ≤ 720p, тут рендер 360p
    assert edit["export_path"].endswith("_clip_v001.mp4") and "exports" in edit["export_path"]
    assert edit["style_id"] == "minidv-concert" and edit["style_level"] == 2


def test_transitions_only_on_accents(built):
    settings, conn, p = built
    edit = conn.execute("SELECT * FROM edits").fetchone()
    tl = json.loads((settings.library_path / edit["timeline_path"]).read_text(encoding="utf-8"))
    cuts = len(tl["clips"]) - 1
    assert 1 <= len(tl["transitions"]) <= 0.2 * cuts  # 80–90% склейок різкі
    accents = [m["t"] for m in tl["markers"]] + [i["end"] for i in tl["inserts"]]
    for t in tl["transitions"]:
        assert min(abs(t["t"] - a) for a in accents) <= 0.5 / tl["fps"]


def test_api_and_reedit_without_gemini(built):
    settings, conn, p = built
    before = conn.execute("SELECT COUNT(*) FROM gemini_requests").fetchone()[0]
    with TestClient(create_app(settings, start_worker=False)) as client:
        edits = client.get(f"/api/projects/{p['id']}/edits").json()
        assert edits[0]["summary"]["inserts"] >= 2
        assert client.get(f"/api/edits/{edits[0]['id']}/preview").status_code == 200
        r = client.put(f"/api/projects/{p['id']}/clip-settings", json={"length": 30, "inserts": False})
        assert r.json()["length"] == 30
        assert client.post(f"/api/projects/{p['id']}/reedit").status_code == 202
    w = Worker(settings)
    while w.run_one(conn):
        pass
    v2 = conn.execute("SELECT * FROM edits WHERE version = 2").fetchone()
    s2 = json.loads(v2["summary"])
    assert 28 <= s2["duration"] <= 32 and s2["inserts"] == 0
    assert conn.execute("SELECT COUNT(*) FROM gemini_requests").fetchone()[0] == before


def test_davinci_package(built):
    import xml.etree.ElementTree as ET
    from fractions import Fraction
    from urllib.parse import unquote

    from render.fcpxml import frame_duration

    settings, conn, p = built
    edit = conn.execute("SELECT * FROM edits WHERE version = 1").fetchone()
    assert ("export_davinci", "done") in {(j["kind"], j["status"]) for j in conn.execute("SELECT * FROM jobs")}
    xml_path = settings.library_path / edit["davinci_path"]
    root_dir = xml_path.parent
    tl = json.loads((settings.library_path / edit["timeline_path"]).read_text(encoding="utf-8"))
    root = ET.parse(xml_path).getroot()

    # без offline media: кожен відносний шлях існує всередині папки проєкту
    for rep in root.iter("media-rep"):
        f = (root_dir / unquote(rep.get("src"))).resolve()
        assert f.exists() and root_dir.resolve() in f.parents
    # оригінали — ті самі байти (жорстке посилання або копія), бібліотека не змінена
    for f in (root_dir / "media" / "originals").iterdir():
        mid = int(f.name.split("_")[0])
        orig = settings.library_path / conn.execute("SELECT original_path FROM media WHERE id = ?", (mid,)).fetchone()[0]
        assert f.stat().st_size == orig.stat().st_size

    spine = root.find(".//spine").findall("asset-clip")
    assert len(spine) == len(tl["clips"]) and all(c.get("enabled") == "0" for c in spine)
    v2 = [c for c in root.iter("asset-clip") if c.get("lane") == "1"]
    assert len(v2) == len(tl["clips"]) and all(c.get("enabled") is None for c in v2)
    assert len([c for c in root.iter("asset-clip") if c.get("lane") == "-1"]) == 1
    assert len([c for c in root.iter("asset-clip") if c.get("lane") == "-2"]) == len(tl["inserts"])
    assert len(list(root.iter("marker"))) == len(tl["markers"])

    # усі часи послідовності — цілі кадри
    fd = frame_duration(tl["fps"])
    for c in spine:
        for attr in ("offset", "duration"):
            v = Fraction(c.get(attr)[:-1])
            assert (v / fd).denominator == 1, (attr, v)
    assert Fraction(root.find(".//sequence").get("duration")[:-1]) / fd == round(tl["duration"] * tl["fps"])

    # стилізовані файли — з запасом: довші за свій кліп і не довші за кліп + 2 с
    for clip_el in v2:
        asset = root.find(f".//asset[@id='{clip_el.get('ref')}']")
        f = root_dir / unquote(asset.find("media-rep").get("src"))
        frames = int(_probe(f)["video"]["nb_read_frames"])
        clip_frames = Fraction(clip_el.get("duration")[:-1]) / fd
        assert clip_frames <= frames <= clip_frames + 2 * tl["fps"]
    assert (root_dir / f"{xml_path.stem}_абсолютні.fcpxml").exists()


def test_vertical_reels(built):
    settings, conn, p = built
    row = conn.execute("SELECT * FROM edits WHERE kind = 'vertical'").fetchone()
    assert row is not None, list(conn.execute("SELECT kind, status, error FROM jobs"))
    reels = json.loads(row["summary"])["reels"]
    assert len(reels) >= 2
    seen = set()
    for r in reels:
        st = _probe(settings.library_path / r["path"])
        w, h = settings.render_size
        assert (st["video"]["width"], st["video"]["height"]) == (h, w)  # 9:16
        assert st["video"]["avg_frame_rate"] == "30/1"
        assert 0 < float(st["video"]["duration"]) <= 25.05
        assert "audio" in st
        assert any(pc["role"] == "peak" for pc in r["pieces"])
        assert {pc["framing"] for pc in r["pieces"]} <= {"crop", "fit", "native"}
        for pc in r["pieces"]:
            for a in np.arange(pc["src_in"], pc["src_in"] + pc["duration"] - 1e-6, 0.25):
                key = (pc["media_id"], round(float(a), 2))
                assert key not in seen
                seen.add(key)
    assert any(pc["framing"] == "crop" for r in reels for pc in r["pieces"])


def test_vertical_settings_api(built):
    settings, conn, p = built
    with TestClient(create_app(settings, start_worker=False)) as client:
        r = client.put(f"/api/projects/{p['id']}/vertical-settings", json={"audio": "none", "count": 1})
        assert r.json() == {"count": 1, "length": 20.0, "audio": "none", "aspect": "9:16"}
        assert client.put(f"/api/projects/{p['id']}/vertical-settings", json={"audio": "loud"}).status_code == 422
        assert client.put(f"/api/projects/{p['id']}/vertical-settings", json={"length": 40}).status_code == 422
        edit = conn.execute("SELECT id FROM edits WHERE kind = 'vertical'").fetchone()["id"]
        assert client.get(f"/api/edits/{edit}/reels/1").status_code == 200
        assert client.get(f"/api/edits/{edit}/reels/99").status_code == 404


def test_more_crowd_by_words_changes_clip_without_gemini(built):
    settings, conn, p = built

    def crowd_share(edit_row):
        tl = json.loads((settings.library_path / edit_row["timeline_path"]).read_text(encoding="utf-8"))
        dur = sum(c["end"] - c["start"] for c in tl["clips"])
        crowd = sum(c["end"] - c["start"] for c in tl["clips"] if c["kind"] == "insert" or "натовп" in c["tags"])
        return crowd / dur

    before = conn.execute("SELECT * FROM edits WHERE kind = 'clip' ORDER BY version DESC").fetchone()
    gemini_before = conn.execute("SELECT COUNT(*) FROM gemini_requests").fetchone()[0]
    spend_before = conn.execute("SELECT COUNT(*) FROM spend").fetchone()[0]
    with TestClient(create_app(settings, start_worker=False)) as client:
        r = client.post(f"/api/projects/{p['id']}/requests", json={"text": "більше натовпу"}).json()
        assert r["source"] == "local"
        assert "edit_project" in client.post(f"/api/requests/{r['id']}/apply", json={"scope": "here"}).json()["jobs"]
    w = Worker(settings)
    while w.run_one(conn):
        pass
    after = conn.execute("SELECT * FROM edits WHERE kind = 'clip' ORDER BY version DESC").fetchone()
    assert after["version"] == before["version"] + 1
    assert crowd_share(after) > crowd_share(before) + 0.1, (crowd_share(before), crowd_share(after))
    assert conn.execute("SELECT COUNT(*) FROM gemini_requests").fetchone()[0] == gemini_before
    assert conn.execute("SELECT COUNT(*) FROM spend").fetchone()[0] == spend_before


def test_clip_and_reels_in_other_formats(built):
    """Кліп 9:16 і ролики 4:5: розміри правильні, кадрування спрацювало."""
    from analysis import framing

    from edit.build import edit_project
    from edit.verticals import vertical_project
    from render.frame import size_for
    settings, conn, p = built
    mp = pytest.MonkeyPatch()
    mp.setattr(framing, "make_detector", lambda models_dir: FakeFaceDetector())
    try:
        edit_project(conn, settings, {"project_id": p["id"], "params": {"aspect": "9:16", "length": 8.0}}, lambda x: None)
        vertical_project(conn, settings, {"project_id": p["id"], "params": {"aspect": "4:5", "count": 1}}, lambda x: None)
    finally:
        mp.undo()
    clip = conn.execute("SELECT * FROM edits WHERE kind = 'clip' ORDER BY version DESC LIMIT 1").fetchone()
    st = _probe(settings.library_path / clip["export_path"])
    assert (st["video"]["width"], st["video"]["height"]) == size_for("9:16", settings.render_height)
    assert json.loads(clip["params"])["aspect"] == "9:16"
    reel = json.loads(conn.execute("SELECT summary FROM edits WHERE kind = 'vertical' ORDER BY version DESC LIMIT 1"
                                   ).fetchone()[0])["reels"][0]
    st = _probe(settings.library_path / reel["path"])
    assert (st["video"]["width"], st["video"]["height"]) == size_for("4:5", settings.render_height)
