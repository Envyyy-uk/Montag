import json
import subprocess

import numpy as np
import pytest

from analysis.track import TrackAnalysis, analyze_file
from edit import audio_mix, inserts, rhythm
from edit.select import Shot, Weights, choose
from edit.track_window import best_window, window_from_bounds


@pytest.fixture(scope="module")
def song_analysis(tmp_path_factory):
    from tests import media

    p = media.song(tmp_path_factory.mktemp("song") / "song.mp3")  # 120 bpm: 8 low, 16 high, 8 mid, 16 high, 4 low
    return analyze_file(p)


# ---------- трек ----------

def test_track_tempo_downbeats_sections(song_analysis):
    a = song_analysis
    assert abs(a.tempo - 120) < 1.5
    assert a.downbeats[:3] == [0, 4, 8]
    beats = np.array(a.beats)
    grid = np.arange(len(beats)) * 0.5
    assert np.abs(beats[:200] - grid[:200]).max() < 0.035  # < 1 кадру при 30 fps
    assert a.drops == [8, 32]
    assert a.choruses == [(8, 24), (32, 48)]
    assert a.bar_level[:8] == ["low"] * 8 and set(a.bar_level[24:32]) == {"mid"}


def test_track_roundtrip(song_analysis, tmp_path):
    song_analysis.save(tmp_path / "t.json")
    assert TrackAnalysis.load(tmp_path / "t.json") == song_analysis


def test_best_window_energetic_and_on_downbeats(song_analysis):
    w = best_window(song_analysis, 60)
    assert 58 <= w.duration <= 62
    assert w.first_beat in song_analysis.downbeats
    assert w.start >= 14  # тихий вступ (0–16 с) не береться
    assert w.last_beat in song_analysis.downbeats or w.last_beat == len(song_analysis.beats)


def test_window_from_user_bounds_snaps(song_analysis):
    w = window_from_bounds(song_analysis, 10.3, 41.1)
    assert w.start == pytest.approx(10.0, abs=0.04) and w.end == pytest.approx(42.0, abs=0.04)


# ---------- ритм ----------

def test_rhythm_by_energy(song_analysis):
    a = song_analysis
    w = window_from_bounds(a, 0, 104)
    sl = rhythm.slots(a, w)
    assert sl[0].start == 0 and abs(sl[-1].end - w.duration) < 1e-9
    for x, y in zip(sl, sl[1:]):
        assert x.end == y.start and x.last_beat == y.first_beat
    by = {lvl: {s.beats for s in sl if s.level == lvl} for lvl in ("low", "mid", "high")}
    assert by == {"low": {4}, "mid": {2}, "high": {1}}
    assert all(s.accent for s in sl if s.level == "high")


def test_rhythm_override(song_analysis):
    w = window_from_bounds(song_analysis, 16, 48)
    sl = rhythm.slots(song_analysis, w, {"high": 2})
    assert {s.beats for s in sl if s.level == "high"} == {2}


# ---------- вибір кадрів ----------

def _slot(a, b, level="mid"):
    return rhythm.Slot(a, b, a * 0.5, b * 0.5, level, False)


def test_select_diversity_and_no_split_needed():
    pool = [Shot(1, 0, 4, 8.5 if i == 0 else 8, scene=(1, i), camera="A", chrono=i / 5, tags=["натовп"])
            for i in range(3)] + [Shot(2, 0, 4, 7, scene=(2, 0), camera="B", chrono=0.5, tags=["сцена"])]
    w = Weights()
    ch = choose([_slot(0, 2), _slot(2, 4), _slot(4, 6), _slot(6, 8)], pool, 4.0, w)
    scenes = [c.shot.scene for c in ch]
    assert all(a != b for a, b in zip(scenes, scenes[1:]))
    assert "B" in {c.shot.camera for c in ch}


def test_hero_holds_up_to_two_bars():
    pool = [Shot(1, 0, 10, 9.6, scene=(1, 0)), Shot(2, 0, 3, 5, scene=(2, 0))]
    slots = [_slot(i, i + 1, "high") for i in range(12)]
    ch = choose(slots, pool, 6.0)
    assert ch[0].hero and ch[0].slot.beats == 8
    assert sum(c.slot.beats for c in ch) == 12


def test_slot_split_when_material_short():
    pool = [Shot(1, 0, 1.2, 7, scene=(1, i)) for i in range(4)]
    ch = choose([_slot(0, 4, "low")], pool, 2.0)  # 2 с слот, а шматки по 1,2 с
    assert len(ch) == 2 and all(c.slot.end - c.slot.start <= pool[0].usable + 1e-9 for c in ch)


def test_clip_never_touches_segment_edges():
    pool = [Shot(1, 10, 13, 9, scene=(1, 0)), Shot(2, 0, 3, 8, scene=(2, 0))]
    for c in choose([_slot(0, 4), _slot(4, 8)], pool, 4.0):  # слоти по 2 с, шматки по 3 с
        assert c.src_in >= c.shot.start + 2 / 30 - 1e-9
        assert c.src_in + (c.slot.end - c.slot.start) <= c.shot.end - 2 / 30 + 1e-9


def test_tag_weights_shift_choice():
    pool = [Shot(1, 0, 4, 8, scene=(1, 0), tags=["сцена"]), Shot(2, 0, 4, 7, scene=(2, 0), tags=["натовп"])]
    plain = choose([_slot(0, 2)], [Shot(**{**s.__dict__}) for s in pool], 1.0)
    crowd = choose([_slot(0, 2)], [Shot(**{**s.__dict__}) for s in pool], 1.0, Weights(tag_weights={"натовп": 0.5}))
    assert plain[0].shot.tags == ["сцена"] and crowd[0].shot.tags == ["натовп"]


# ---------- вставки ----------

def test_insert_rules(song_analysis):
    a = song_analysis
    w = best_window(a, 60)
    evs = [inserts.CrowdEvent(1, t, t + 1.5, 10 + i, chrono=i / 9, media_duration=200) for i, t in enumerate(range(5, 200, 20))]
    placed = inserts.plan(a, w, evs)
    assert 2 <= len(placed) <= 4
    for p in placed:
        assert p.first_beat in a.downbeats
        assert 1.0 - 1e-6 <= p.end - p.start <= 3.0 + 1e-6
    for x, y in zip(placed, placed[1:]):
        assert y.start - x.end >= 8 - 1e-6
    assert len(inserts.plan(a, w, evs, max_count=1)) == 1
    assert inserts.plan(a, w, [inserts.CrowdEvent(1, 3, 3.3, 20)]) == []  # коротший за 0,5 с


# ---------- звук ----------

def test_duck_envelope_shape():
    sr = 1000
    env = audio_mix.duck_envelope(10 * sr, [(4.0, 6.0)], -12, sr=sr)
    low = 10 ** (-12 / 20)
    assert env[2 * sr] == pytest.approx(1)
    assert env[int(3.85 * sr)] == pytest.approx(1, abs=1e-3)          # до атаки
    assert env[int(3.925 * sr)] == pytest.approx((1 + low) / 2, abs=0.01)  # середина атаки 150 мс
    assert env[5 * sr] == pytest.approx(low, abs=1e-4)
    assert env[int(6.075 * sr)] == pytest.approx((1 + low) / 2, abs=0.01)
    assert env[int(6.2 * sr)] == pytest.approx(1)


def test_mix_levels():
    sr = audio_mix.SR
    track = np.full((sr * 4, 2), 0.1, np.float32)
    live = np.full((sr, 2), 0.3, np.float32)
    out = audio_mix.mix(track, [(2.0, live)], -12, 0)
    mid = out[int(2.5 * sr)]
    expect = 0.1 * 10 ** (-12 / 20) + 0.1  # живий вирівняний до RMS треку
    assert mid[0] == pytest.approx(expect, rel=0.02)
    assert out[int(0.5 * sr)][0] == pytest.approx(0.1, rel=1e-3)


def test_hero_cooldown_and_top_share():
    pool = [Shot(i, 0, 10, 9.5, scene=(i, 0)) for i in range(20)]
    slots = [_slot(i, i + 1, "high") for i in range(32)]
    heroes = [c for c in choose(slots, pool, 16.0) if c.hero]
    assert 1 <= len(heroes) <= 2
    for a, b in zip(heroes, heroes[1:]):
        assert b.slot.first_beat - a.slot.first_beat >= 16


def test_insert_avoids_drop(song_analysis):
    a = song_analysis
    w = window_from_bounds(a, 0, 104)
    evs = [inserts.CrowdEvent(1, t, t + 1.5, 10, chrono=0.5, media_duration=200) for t in (10, 40, 70, 90)]
    drop_beats = {a.downbeats[k] for k in a.drops}
    for p in inserts.plan(a, w, evs):
        assert not any(p.first_beat <= b < p.last_beat for b in drop_beats)


def test_decode_silent_video_gives_silence(tmp_path):
    from tests.media import ff

    p = tmp_path / "silent.mp4"
    ff("-f", "lavfi", "-i", "testsrc2=s=160x90:r=30:d=2", "-c:v", "libx264", "-preset", "ultrafast", str(p))
    x = audio_mix.decode_stereo(p, 0.5, 1.0)
    assert x.shape == (audio_mix.SR, 2) and not x.any()
