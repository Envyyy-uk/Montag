import json
import subprocess

from analysis.probe import MediaInfo, probe
from render.proxy import EDIT, GEMINI, build_command, build_filter, make_proxy, target_size

ALL = frozenset({"zscale", "tonemap", "scale", "fps", "format"})


def test_target_size():
    assert target_size(3840, 2160, 1080) == (1920, 1080)
    assert target_size(2160, 3840, 1080) == (1080, 1920)
    assert target_size(1280, 720, 1080) == (1280, 720)  # без збільшення
    assert target_size(1920, 1080, 480) == (854, 480)
    assert target_size(1440, 1080, 480) == (640, 480)


def test_hdr_filter_uses_tonemap():
    info = MediaInfo(duration=1, has_video=True, has_audio=False, width=3840, height=2160, fps=60, hdr=True)
    vf, warnings = build_filter(info, GEMINI, ALL)
    assert vf.startswith("zscale=t=linear")
    assert "tonemap=tonemap=hable" in vf
    assert "scale=854:480" in vf and "fps=15" in vf
    assert not warnings


def test_hdr_without_zscale_warns():
    info = MediaInfo(duration=1, has_video=True, has_audio=False, width=100, height=100, hdr=True)
    vf, warnings = build_filter(info, EDIT, frozenset())
    assert "zscale" not in vf and warnings


def test_rotated_uses_display_size():
    info = MediaInfo(duration=1, has_video=True, has_audio=False, width=3840, height=2160, rotation=90)
    vf, _ = build_filter(info, EDIT, ALL)
    assert "scale=1080:1920" in vf


def test_nvenc_command():
    info = MediaInfo(duration=1, has_video=True, has_audio=True, width=1920, height=1080, fps=30)
    args, _ = build_command("in.mp4", "out.mp4", info, EDIT, ALL, nvenc=True)
    assert "h264_nvenc" in args and "-cq" in args


def _stream(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return next(s for s in json.loads(out)["streams"] if s["codec_type"] == "video")


def test_real_proxies(media_dir, tmp_path):
    src = media_dir / "concert.mp4"
    info, _ = probe(src)
    seen = []
    make_proxy(src, tmp_path / "e.mp4", info, EDIT, seen.append, nvenc=False)
    make_proxy(src, tmp_path / "g.mp4", info, GEMINI, nvenc=False)
    e, g = _stream(tmp_path / "e.mp4"), _stream(tmp_path / "g.mp4")
    assert (e["width"], e["height"]) == (1280, 720)
    assert (g["width"], g["height"]) == (854, 480)
    assert g["avg_frame_rate"] == "15/1"
    assert seen and seen[-1] > 0.5
    assert not list(tmp_path.glob(".*part*"))


def test_real_hdr_to_sdr(media_dir, tmp_path):
    src = media_dir / "iphone_hdr.mov"
    info, _ = probe(src)
    make_proxy(src, tmp_path / "e.mp4", info, EDIT, nvenc=False)
    s = _stream(tmp_path / "e.mp4")
    assert s["codec_name"] == "h264" and s["pix_fmt"] == "yuv420p"
    assert s.get("color_transfer") == "bt709" and s.get("color_primaries") == "bt709"
