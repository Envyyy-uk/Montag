from analysis.probe import parse, probe


def test_probe_sdr(media_dir):
    info, raw = probe(media_dir / "concert.mp4")
    assert info.has_video and info.has_audio
    assert (info.width, info.height) == (1280, 720)
    assert info.fps == 30
    assert info.codec == "h264"
    assert not info.hdr
    assert info.orientation == "horizontal"
    assert info.shot_at == "2025-06-14T21:03:05Z"
    assert abs(info.duration - 2) < 0.1


def test_probe_vertical(media_dir):
    info, _ = probe(media_dir / "vertical.mp4")
    assert info.orientation == "vertical"


def test_probe_hdr(media_dir):
    info, _ = probe(media_dir / "iphone_hdr.mov")
    assert info.hdr
    assert info.codec == "hevc"
    assert info.bit_depth == 10
    assert info.color_transfer == "arib-std-b67"


def test_probe_audio(media_dir):
    info, _ = probe(media_dir / "track.mp3")
    assert not info.has_video and info.has_audio
    assert info.orientation == "none"


def test_iphone_metadata_rotation_and_date():
    raw = {
        "format": {"duration": "12.5", "tags": {
            "creation_time": "2025-06-14T18:03:05.000000Z",
            "com.apple.quicktime.creationdate": "2025-06-14T21:03:05+0300"}},
        "streams": [{
            "codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160,
            "avg_frame_rate": "30000/1001", "pix_fmt": "yuv420p10le",
            "color_transfer": "arib-std-b67", "color_primaries": "bt2020",
            "side_data_list": [{"side_data_type": "Display Matrix", "rotation": -90}]}],
    }
    info = parse(raw)
    assert info.rotation == 270
    assert (info.display_width, info.display_height) == (2160, 3840)
    assert info.orientation == "vertical"
    assert info.fps == 29.97
    assert info.hdr
    assert info.shot_at == "2025-06-14T18:03:05Z"


def test_bogus_camera_date_ignored():
    raw = {"format": {"duration": "1", "tags": {"creation_time": "1970-01-01T00:00:00.000000Z"}},
           "streams": [{"codec_type": "video", "width": 10, "height": 10}]}
    assert parse(raw).shot_at is None


def test_attached_picture_is_not_video():
    raw = {"format": {"duration": "3"}, "streams": [
        {"codec_type": "audio", "codec_name": "mp3", "sample_rate": "44100"},
        {"codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}}]}
    assert not parse(raw).has_video


def test_embedded_timecode(tmp_path):
    from tests.media import ff

    p = tmp_path / "tc.mov"
    ff("-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=1", "-c:v", "libx264", "-preset", "ultrafast",
       "-timecode", "21:03:05:12", str(p))
    info, _ = probe(p)
    assert info.timecode == "21:03:05:12"
    from fractions import Fraction

    from render.fcpxml import timecode_seconds
    assert timecode_seconds(info.timecode, info.fps) == Fraction(21 * 3600 + 3 * 60 + 5) + Fraction(12, 30)
