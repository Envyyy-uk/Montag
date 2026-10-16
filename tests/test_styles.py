import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from edit.timeline import Clip, Insert, Marker, Timeline
from edit.transitions_plan import plan
from render.clip_render import Source, clip_graph, render_clip_file
from render.looks import get_style, list_styles, look_filters, stamp_filters
from render.transitions import TRANSITIONS


def _frames(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                          "stream=nb_read_frames,width,height", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)["streams"][0]


@pytest.fixture(scope="module")
def src(tmp_path_factory):
    from tests import media

    p = media.sdr_video(tmp_path_factory.mktemp("st") / "src.mp4", w=640, h=360, dur=4)
    return Source(p, 640, 360, "2025-06-14T18:03:05Z")


def test_four_starter_styles_with_three_levels():
    ids = {s.id for s in list_styles()}
    assert ids == {"minidv-concert", "summer-trip", "vhs-90s", "clean"}
    for s in list_styles():
        assert set(s.data["levels"]) == {"1", "2", "3"}
        assert set(s.transitions["allowed"]) <= set(TRANSITIONS)
        assert s.data["filter"] in ("lofi", "standard")


def test_minidv_look_has_required_elements():
    look = get_style("minidv-concert").look(2)
    chain = ",".join(look_filters(look, 1920, 1080))
    assert "scale=-2:480" in chain                 # м'якість через 480 рядків
    assert "noise=" in chain and "c1s=" in chain   # зерно + кольоровий шум
    assert "saturation=0.850" in chain             # знижена насиченість
    assert "bs=0.080" in chain                     # холодно-ціанові тіні
    assert "drawgrid" in chain and "vignette" in chain
    stamp = stamp_filters(look, 1080, "2025-06-14T18:03:05Z", 1.5)
    assert len(stamp) == 2 and "JUN 14 2025" in stamp[0] and "localtime" in stamp[1]
    assert "fontcolor=#ffd23a" in stamp[0]


def test_levels_increase_intensity():
    s = get_style("minidv-concert")
    l1, l3 = s.look(1), s.look(3)
    assert l1["grain"] < s.look(2)["grain"] < l3["grain"]
    assert l1["scanlines"] < l3["scanlines"]


def test_no_stamp_without_date():
    assert stamp_filters(get_style("minidv-concert").look(2), 1080, None, 0) == []
    assert stamp_filters(get_style("clean").look(2), 1080, "2025-06-14T18:03:05Z", 0) == []


@pytest.mark.parametrize("style_id", ["minidv-concert", "summer-trip", "vhs-90s", "clean"])
def test_style_renders_exact_frames(src, tmp_path, style_id):
    out = tmp_path / "o.mp4"
    render_clip_file(src, 0.5, 30, 30, out, get_style(style_id).look(3), (640, 360), nvenc=False)
    st = _frames(out)
    assert int(st["nb_read_frames"]) == 30 and (st["width"], st["height"]) == (640, 360)


@pytest.mark.parametrize("kind", list(TRANSITIONS))
def test_each_transition_both_edges_exact_frames(src, tmp_path, kind):
    out = tmp_path / f"{kind}.mp4"
    t = TRANSITIONS[kind]
    edges = {"kind_out": kind, "t_out": 0.5}
    if t.in_beats > 0:
        edges.update(kind_in=kind, t_in=0.4)
    render_clip_file(src, 0.2, 45, 30, out, get_style("minidv-concert").look(2), (640, 360), nvenc=False, **edges)
    assert int(_frames(out)["nb_read_frames"]) == 45


def test_vertical_source_gets_blurred_background():
    g = clip_graph(Source("x.mp4", 1080, 1920), 0, 30, 30, 1920, 1080, {})
    assert "gblur" in g and "overlay" in g
    g2 = clip_graph(Source("x.mp4", 1440, 1080), 0, 30, 30, 1920, 1080, {})
    assert "overlay" not in g2  # 4:3 — просто кадрування


def _tl(n=20, fps=30.0):
    clips = [Clip(i * 0.5, (i + 1) * 0.5, 1, 0) for i in range(n)]
    return Timeline(fps=fps, duration=n * 0.5, track_media_id=1, track_in=0, track_out=n * 0.5, clips=clips,
                    markers=[Marker(2.0, "drop"), Marker(6.0, "chorus")], inserts=[Insert(4.0, 5.0, 1, 0)],
                    beat=0.5)


def test_plan_only_accents_and_share():
    tl = _tl()
    chosen = plan(tl, ["flash", "zoom", "glitch"], 0.15, seed=1)
    assert 1 <= len(chosen) <= int(0.15 * 19) or len(chosen) == 1
    assert {c["t"] for c in chosen} <= {2.0, 4.0, 5.0, 6.0}
    assert chosen[0]["t"] == 2.0  # дроп — першим
    assert plan(tl, ["flash"], 0.15, disabled={"flash"}) == []


def test_plan_skips_structural_on_short_or_insert():
    tl = _tl()
    tl.clips[3].duration  # noqa: B018
    chosen = plan(tl, ["freeze"], 0.5, seed=0)
    for c in chosen:
        assert c["out"] * tl.fps >= 2


def test_styles_api_and_test_job(settings):
    from core.db import open_db
    from server.app import create_app
    from worker.runner import Worker

    settings = settings.model_copy(update={"render_height": 240})
    with TestClient(create_app(settings, start_worker=False)) as client:
        styles = client.get("/api/styles").json()
        assert {s["id"] for s in styles} >= {"minidv-concert"}
        assert client.post("/api/styles/nope/test", json={}).status_code == 404
        assert client.post("/api/styles/vhs-90s/test", json={}).status_code == 202
        conn = open_db(settings.database_path)
        Worker(settings).run_one(conn)
        for lvl in (1, 2, 3):
            r = client.get(f"/api/styles/vhs-90s/test/{lvl}")
            assert r.status_code == 200 and len(r.content) > 1000
        assert client.post("/api/styles/vhs-90s/approve", json={"level": 3}).status_code == 200
        assert client.post("/api/styles/vhs-90s/approve", json={"level": 4}).status_code == 422
        vhs = next(s for s in client.get("/api/styles").json() if s["id"] == "vhs-90s")
        assert vhs["approved_level"] == 3 and all(vhs["tests"].values())
        pid = client.post("/api/projects", json={"name": "X"}).json()["id"]
        assert client.put(f"/api/projects/{pid}/style", json={"style_id": "vhs-90s"}).status_code == 200
        from render import style_store
        style, level = style_store.resolve(conn, settings, "vhs-90s")
        assert (style.id, level) == ("vhs-90s", 3)
