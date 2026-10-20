import re

import numpy as np
import pytest

from analysis import framing
from analysis.framing import Box, decide
from edit import moments
from edit.select import Shot

W16 = (1080 * 9 / 16) / 1920  # ширина вікна 9:16 у кадрі 16:9 ≈ 0.316


def face(cx, w=0.08, score=0.9):
    return Box(cx - w / 2, 0.3, cx + w / 2, 0.3 + w * 1.8, score, "face")


def samples(xs, fps=5):
    return [(i / fps, [face(x)] if x is not None else []) for i, x in enumerate(xs)]


def test_crop_follows_subject_smoothly():
    xs = [0.3] * 5 + list(np.linspace(0.3, 0.6, 20)) + [0.6] * 10  # людина повільно йде сценою
    fr = decide(samples(xs), W16)
    assert fr.mode == "crop"
    cs = [c for _, c in fr.keys]
    assert all(W16 / 2 - 1e-6 <= c <= 1 - W16 / 2 + 1e-6 for c in cs)
    assert abs(cs[0] - 0.3) < 0.01 and cs[-1] > 0.55            # рухається за обличчям
    steps = np.abs(np.diff(cs))
    assert steps.max() <= framing.MAX_SPEED / 5 + 1e-6          # без ривків


def test_two_people_at_edges_fall_back_to_blur():
    s = [(i / 5, [face(0.1), face(0.9)]) for i in range(10)]
    fr = decide(s, W16)
    assert fr.mode == "fit" and "не влазить" in fr.reason


def test_no_subject_uses_gemini_hint_or_blur():
    assert decide(samples([None] * 5), W16).mode == "fit"
    fr = decide(samples([None] * 5), W16, hint_x=0.62)
    assert fr.mode == "crop" and abs(fr.keys[0][1] - 0.62) < 1e-6


def test_vertical_source_is_native():
    assert framing.analyze_clip(None, None, 0, 1, 1080, 1920).mode == "native"


def test_fast_subject_that_window_cannot_keep_falls_back():
    xs = [0.2, 0.8] * 10  # стрибає швидше, ніж дозволено вікну — обличчя обрізалось би
    assert decide(samples(xs), W16).mode == "fit"


def _eval_expr(expr: str, t: float, iw: float = 1.0) -> float:
    py = expr.replace("\\,", ",").replace("if(", "_if(").replace("lt(", "_lt(")
    return eval(py, {"_if": lambda c, a, b: a if c else b, "_lt": lambda a, b: a < b, "t": t, "iw": iw})


def test_crop_expr_piecewise_linear():
    keys = [(0.0, 0.3), (1.0, 0.5), (2.0, 0.5)]
    e = framing.crop_x_expr(keys, 0.2)
    assert _eval_expr(e, 0.0) == pytest.approx(0.2)
    assert _eval_expr(e, 0.5) == pytest.approx(0.3)
    assert _eval_expr(e, 1.5) == pytest.approx(0.4)
    assert _eval_expr(e, 5.0) == pytest.approx(0.4)


def test_mediapipe_detector_smoke(tmp_path):
    det = framing.make_detector(tmp_path / "models")
    if det is None:
        pytest.skip("MediaPipe недоступний у цьому середовищі")
    try:
        assert det.detect(np.zeros((360, 640, 3), np.uint8)) == []
    finally:
        det.close()


# ---------- моменти ----------

def _pool():
    pool = []
    for m in range(3):
        for i in range(20):
            score = 9.5 if i == 12 else 6.0 + (i % 3) * 0.3
            pool.append(Shot(m + 1, i * 3.0, i * 3.0 + 3.0, score, scene=(m + 1, i // 4),
                             burst=12 if i == 12 else 0, chrono=(m * 20 + i) / 59))
    return pool


def test_moments_three_reels_structure():
    ms = moments.choose(_pool(), 3, 20)
    assert len(ms) == 3
    seen = set()
    for m in ms:
        assert 15 <= m.duration <= 25.0001
        roles = [p.role for p in m.pieces]
        assert "peak" in roles and roles[0] in ("buildup", "peak")
        tail = [p for p in m.pieces if p.role == "tail"]
        assert not tail or tail[0].duration <= 2.0
        for p in m.pieces:
            for a in np.arange(p.src_in, p.src_in + p.duration - 1e-6, 0.5):
                key = (p.media_id, round(float(a), 1))
                assert key not in seen  # ролики не перетинаються
                seen.add(key)
    chronos = [m.peak.chrono for m in ms]
    assert chronos == sorted(chronos) and min(np.diff(chronos)) >= 1 / 6 - 1e-9


def test_moment_buildup_is_continuous_footage():
    m = moments.choose(_pool(), 1, 20)[0]
    assert m.peak.score == 9.5
    # розгін + пік + хвіст із тієї ж зйомки злилися в один безперервний шматок
    assert len(m.pieces) == 1 and m.pieces[0].role == "peak"
    p = m.pieces[0]
    assert p.src_in + p.duration == pytest.approx(39 + 1.5)  # пік 36–39 + 1,5 с після
    assert p.duration == pytest.approx(20)


def test_moments_skip_stubby_reels_when_material_runs_out():
    small = [s for s in _pool() if s.media_id == 1][:9]  # 27 с матеріалу на 3 ролики по 15+
    ms = moments.choose(small, 3, 15)
    assert ms and all(m.duration >= 14.5 for m in ms)
    assert len(moments.choose(small[:3], 3, 20)) == 1  # зовсім мало — один ролик, а не нуль


# ---------- формати кадру ----------

@pytest.mark.parametrize("src", [(640, 360), (360, 640)])
@pytest.mark.parametrize("aspect", ["16:9", "9:16", "1:1", "4:5"])
@pytest.mark.parametrize("mode", ["auto", "crop", "blur"])
def test_frame_graph_every_format(tmp_path, src, aspect, mode):
    import json as _json
    import subprocess

    from analysis.framing import Framing
    from render.frame import frame_graph, size_for
    w, h = size_for(aspect, 240)
    fr = Framing("crop", [(0.0, 0.3), (1.0, 0.7)]) if src[0] > src[1] else None
    out = tmp_path / "o.mp4"
    g = frame_graph(src[0], src[1], w, h, "0:v", "v", fr, mode)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s={src[0]}x{src[1]}:d=1",
                    "-filter_complex", g, "-map", "[v]", "-pix_fmt", "yuv420p", str(out)], check=True)
    st = _json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                     "stream=width,height", "-of", "json", str(out)],
                                    capture_output=True, text=True).stdout)["streams"][0]
    assert (st["width"], st["height"]) == (w, h)


def test_frame_choices():
    from analysis.framing import Framing
    from render.frame import frame_graph, size_for
    assert size_for("4:5", 1080) == (1080, 1350) and size_for("16:9", 1080) == (1920, 1080)
    # вертикальне в 16:9: auto — розмитий фон, crop — смуга по висоті
    assert "gblur" in frame_graph(1080, 1920, 1920, 1080, "0:v", "v", None, "auto")
    assert "gblur" not in frame_graph(1080, 1920, 1920, 1080, "0:v", "v", None, "crop")
    # горизонтальне в 9:16 з обличчями — вікно за обличчям навіть у auto
    g = frame_graph(1920, 1080, 1080, 1920, "0:v", "v", Framing("crop", [(0.0, 0.4)]), "auto")
    assert "crop=w=trunc(ih*" in g and "gblur" not in g
    # 4:3 в 1:1 (помірно) — обрізання; 16:9 у 4:5 без облич — фон; blur — завжди фон
    assert "gblur" not in frame_graph(1440, 1080, 1080, 1080, "0:v", "v", None, "auto")
    assert "gblur" in frame_graph(1920, 1080, 1080, 1350, "0:v", "v", None, "auto")
    assert "gblur" in frame_graph(1440, 1080, 1080, 1080, "0:v", "v", None, "blur")
