import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import pytest

from render import fcpxml
from render.audio_keys import duck_keyframes


def test_rational_times():
    assert fcpxml.rt(Fraction(0)) == "0s"
    assert fcpxml.rt(Fraction(3)) == "3s"
    assert fcpxml.rt(12 * fcpxml.frame_duration(29.97)) == "1001/2500s"
    assert fcpxml.frame_duration(30) == Fraction(1, 30)
    assert fcpxml.frame_duration(23.976) == Fraction(1001, 24000)


def test_timecode_seconds():
    assert fcpxml.timecode_seconds("01:00:00:00", 25) == 3600
    assert fcpxml.timecode_seconds("00:00:01:15", 30) == Fraction(3, 2)
    # drop-frame: 00:01:00;02 — це 1800-й кадр
    assert fcpxml.timecode_seconds("00:01:00;02", 29.97) == 1800 * Fraction(1001, 30000)
    assert fcpxml.timecode_seconds(None, 30) == 0 and fcpxml.timecode_seconds("bad", 30) == 0


def test_duck_keyframes():
    k = duck_keyframes([(4.0, 6.0, -12.0)], base_db=-1.0)
    assert k == [(0.0, -1.0), (3.85, -1.0), (4.0, -13.0), (6.0, -13.0), (6.15, -1.0)]


def _assets(tmp_path):
    for n in ("a.mp4", "b.mp4", "t.mp3"):
        (tmp_path / "media" / n).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "media" / n).write_bytes(b"x")
    return [fcpxml.Asset("o1", "a", tmp_path / "media" / "a.mp4", Fraction(10), True, True, 1920, 1080, 30,
                         start=Fraction(75785)),
            fcpxml.Asset("s1", "b", tmp_path / "media" / "b.mp4", Fraction(4), True, False, 1920, 1080, 30),
            fcpxml.Asset("t", "t", tmp_path / "media" / "t.mp3", Fraction(120), False, True)]


def test_build_structure(tmp_path):
    assets = _assets(tmp_path)
    items = [fcpxml.Item("o1", 0, 30, Fraction(2), lane=0, enabled=False, video_only=True),
             fcpxml.Item("o1", 30, 30, Fraction(5), lane=0, enabled=False, video_only=True),
             fcpxml.Item("s1", 0, 30, Fraction(1), lane=1),
             fcpxml.Item("s1", 30, 30, Fraction(1), lane=1),
             fcpxml.Item("t", 0, 60, Fraction(16), lane=-1, audio_only=True, volume_keys=[(0, 0), (1.0, -12)]),
             fcpxml.Item("o1", 35, 20, Fraction(6), lane=-2, audio_only=True, volume_db=3.5)]
    xml = fcpxml.build("Тест", 30, 1920, 1080, 60, assets, items, [fcpxml.MarkerAt(30, "Дроп")], base=tmp_path)
    root = ET.fromstring(xml)
    assert root.get("version") == "1.9"
    seq = root.find(".//sequence")
    assert seq.get("duration") == "2s"
    spine = seq.find("spine").findall("asset-clip")
    assert len(spine) == 2 and all(c.get("enabled") == "0" and c.get("srcEnable") == "video" for c in spine)
    # таймкод джерела враховано: start = 75785 + 2
    assert spine[0].get("start") == "75787s"
    lanes = [c.get("lane") for c in root.iter("asset-clip") if c.get("lane")]
    assert sorted(lanes) == ["-1", "-2", "1", "1"]
    # приєднаний кліп V2 другого кліпу: offset у локальному часі батька = його start
    v2b = spine[1].find("asset-clip[@lane='1']")
    assert v2b.get("offset") == spine[1].get("start")
    # A2 на кадрі 35 прив'язаний до другого кліпу (30–60): 75790 + 5/30
    a2 = spine[1].find("asset-clip[@lane='-2']")
    assert a2.get("offset") == fcpxml.rt(Fraction(75790) + Fraction(5, 30))
    assert a2.find("adjust-volume").get("amount") == "+3.50dB"
    keys = spine[0].find("asset-clip[@lane='-1']/adjust-volume/param/keyframeAnimation")
    assert len(keys) == 2
    marker = spine[1].find("marker")
    assert marker.get("value") == "Дроп" and marker.get("start") == spine[1].get("start")
    src = root.find(".//asset[@id='o1']/media-rep").get("src")
    assert src == "media/a.mp4" and (tmp_path / src).exists()


def test_absolute_urls(tmp_path):
    xml = fcpxml.build("Т", 30, 1920, 1080, 30, _assets(tmp_path),
                       [fcpxml.Item("o1", 0, 30, Fraction(0), enabled=False)], [], base=None)
    assert 'src="file://' in xml


def test_cyrillic_and_spaces_url_encoded(tmp_path):
    p = tmp_path / "media" / "Концерт 1.mp4"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"x")
    a = fcpxml.Asset("o1", "Концерт 1", p, Fraction(1), True, False, 1920, 1080, 30)
    xml = fcpxml.build("Т", 30, 1920, 1080, 30, [a], [fcpxml.Item("o1", 0, 30, Fraction(0))], [], base=tmp_path)
    src = ET.fromstring(xml).find(".//media-rep").get("src")
    assert " " not in src and "%D0%9A" in src
    from urllib.parse import unquote
    assert (tmp_path / unquote(src)).exists()


def test_empty_spine_rejected(tmp_path):
    with pytest.raises(ValueError):
        fcpxml.build("Т", 30, 1920, 1080, 30, _assets(tmp_path), [], [], base=None)
