import json
import shutil

import numpy as np
import pytest

from analysis import audio, quality, scenes, thresholds
from analysis.probe import probe
from render.proxy import GEMINI, make_proxy


# ---------- пороги ----------

def test_presets_lofi_softer():
    std, lofi = thresholds.from_spec("standard"), thresholds.from_spec("lofi")
    assert lofi.min_sharpness < std.min_sharpness
    assert lofi.max_shake > std.max_shake
    assert lofi.dark_p95 < std.dark_p95


def test_spec_override_and_unknown():
    th = thresholds.from_spec({"preset": "standard", "max_shake": 9})
    assert th.max_shake == 9 and th.min_sharpness == thresholds.presets()["standard"]["min_sharpness"]
    assert thresholds.for_style(None, "lofi") == thresholds.from_spec("lofi")
    with pytest.raises(ValueError):
        thresholds.from_spec("nope")


# ---------- сцени ----------

def test_split_long_scene():
    assert scenes.split_scene(0, 0, 5.5) == [scenes.Piece(0, 0, 5.5)]
    parts = scenes.split_scene(3, 10, 21)  # 11 с → 3 шматки по 3,67
    assert len(parts) == 3 and parts[0].start == 10 and parts[-1].end == 21
    assert all(2 <= p.duration <= 4 for p in parts)
    for length in np.arange(6.01, 60, 0.37):
        assert all(2 <= p.duration <= 4.0001 for p in scenes.split_scene(0, 0, float(length)))


# ---------- метрики і фільтр на синтетичному відео ----------

@pytest.fixture(scope="module")
def analysed(tmp_path_factory):
    from tests import media

    d = tmp_path_factory.mktemp("an")
    src = media.analysis_video(d / "src.mp4")
    info, _ = probe(src)
    proxy = d / "p480.mp4"
    make_proxy(src, proxy, info, GEMINI, nvenc=False)
    pinfo, _ = probe(proxy)
    return proxy, pinfo, quality.measure_frames(proxy, pinfo.duration)


def _reason(fm, a, b, preset="standard"):
    th = thresholds.from_spec(preset)
    m = quality.segment_metrics(fm, a, b)
    return quality.reject_reason(m, b - a, th), m


def test_frames_sampled_at_5fps(analysed):
    _, info, fm = analysed
    assert abs(len(fm.t) - info.duration * quality.SAMPLE_FPS) <= 2


def test_sharp_passes(analysed):
    assert _reason(analysed[2], 0.2, 2.8)[0] is None


def test_dark_rejected(analysed):
    reason, _ = _reason(analysed[2], 3.2, 5.8, "lofi")
    assert reason in ("кишеня", "темрява")


def test_blur_rejected(analysed):
    reason, m = _reason(analysed[2], 6.2, 8.8, "lofi")
    assert reason == "розмито", m


def test_shake_rejected_standard(analysed):
    reason, m = _reason(analysed[2], 9.2, 11.8)
    assert reason == "тряска", m
    assert m["shake"] > _reason(analysed[2], 0.2, 2.8)[1]["shake"] * 3


def test_flat_rejected(analysed):
    assert _reason(analysed[2], 12.2, 14.8, "lofi")[0] == "однотонне"


def test_local_score_orders(analysed):
    fm = analysed[2]
    th = thresholds.from_spec("lofi")
    good = quality.local_score(quality.segment_metrics(fm, 0.2, 2.8), th)
    blur = quality.local_score(quality.segment_metrics(fm, 6.2, 8.8), th)
    assert 0 <= blur < good <= 1


def test_scene_detection_finds_cuts(analysed):
    proxy, info, _ = analysed
    found = scenes.detect_scenes(proxy, info.duration, info.fps)
    starts = [s for s, _ in found]
    # хоча б склейки різке→темрява і однотонне→фрактал
    assert any(abs(s - 3) < 0.3 for s in starts), found
    assert any(abs(s - 15) < 0.3 for s in starts), found


def test_metrics_roundtrip(analysed, tmp_path):
    fm = analysed[2]
    fm.save(tmp_path / "f.npz")
    back = quality.FrameMetrics.load(tmp_path / "f.npz")
    assert np.array_equal(back.sharpness, fm.sharpness)


# ---------- звук ----------

def test_crowd_burst_detected(analysed):
    res = audio.analyze_file(analysed[0])
    crowd = [e for e in res.events if e.kind == "crowd"]
    assert len(crowd) == 1, res.events
    assert abs(crowd[0].start - 19) < 0.4 and abs(crowd[0].end - 21) < 0.6
    assert crowd[0].peak_excess_db >= 6
    assert abs(len(res.envelope_db) - 240) <= 2  # 24 с × 10 Гц
    assert res.db_between(19.5, 20.5) > res.db_between(10, 11) + 6


def test_tonal_burst_is_loud_not_crowd():
    sr = audio.SR
    t = np.arange(sr * 20) / sr
    y = 0.02 * np.sin(2 * np.pi * 220 * t)
    y[sr * 10: sr * 12] = 0.5 * np.sin(2 * np.pi * 440 * t[sr * 10: sr * 12])
    res = audio.analyze_signal(y.astype(np.float32), sr)
    assert [e.kind for e in res.events] == ["loud"]


def test_short_click_ignored():
    sr = audio.SR
    rng = np.random.default_rng(0)
    y = 0.01 * rng.standard_normal(sr * 15)
    y[sr * 7: sr * 7 + sr // 5] *= 40  # 0,2 с
    assert audio.analyze_signal(y.astype(np.float32), sr).events == []


def test_silence():
    assert audio.analyze_signal(np.zeros(audio.SR * 5, dtype=np.float32)).events == []
