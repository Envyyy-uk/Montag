import subprocess

from core import system_check as sc


def fake_run_factory(encoders="", encode_rc=0, smi_out="", smi_rc=0):
    def fake_run(cmd, timeout=30):
        name = cmd[0]
        if "-version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, f"{name} version 7.1-full Copyright", "")
        if "-encoders" in cmd:
            return subprocess.CompletedProcess(cmd, 0, encoders, "")
        if "h264_nvenc" in cmd:
            return subprocess.CompletedProcess(cmd, encode_rc, "", "" if encode_rc == 0 else "No NVENC capable devices found")
        if "nvidia-smi" in name:
            return subprocess.CompletedProcess(cmd, smi_rc, smi_out, "")
        raise AssertionError(cmd)
    return fake_run


def test_all_ok(monkeypatch):
    monkeypatch.setattr(sc.shutil, "which", lambda n: f"/bin/{n}")
    monkeypatch.setattr(sc, "_run", fake_run_factory(
        encoders=" V....D h264_nvenc\n V....D hevc_nvenc\n",
        smi_out="NVIDIA GeForce RTX 4070 Laptop GPU, 560.94, 8188 MiB\n"))
    r = sc.run_all()
    assert r.ok and r.nvenc
    d = r.as_dict()
    assert {c["name"] for c in d["checks"]} == {"ffmpeg", "ffprobe", "nvenc", "cuda"}
    assert "7.1-full" in d["checks"][0]["detail"]
    assert "RTX 4070" in d["checks"][3]["detail"]


def test_no_ffmpeg_is_fatal(monkeypatch):
    monkeypatch.setattr(sc.shutil, "which", lambda n: None)
    r = sc.run_all()
    assert not r.ok
    assert "ПОМИЛКА" in sc.format_report(r)


def test_nvenc_listed_but_broken_is_warning(monkeypatch):
    monkeypatch.setattr(sc.shutil, "which", lambda n: None if n == "nvidia-smi" else f"/bin/{n}")
    monkeypatch.setattr(sc, "_run", fake_run_factory(encoders="h264_nvenc", encode_rc=1))
    r = sc.run_all()
    assert r.ok  # NVENC і CUDA не критичні
    assert not r.nvenc
    nvenc = next(c for c in r.checks if c.name == "nvenc")
    assert "не працює" in nvenc.detail


def test_no_nvenc_encoder(monkeypatch):
    monkeypatch.setattr(sc.shutil, "which", lambda n: f"/bin/{n}")
    monkeypatch.setattr(sc, "_run", fake_run_factory(encoders="libx264"))
    assert "libx264" in sc.check_nvenc().detail


def test_real_ffmpeg_smoke():
    """На машині без GPU NVENC має чесно звітувати про відсутність, без винятків."""
    r = sc.run_all()
    assert isinstance(r.as_dict()["ok"], bool)
