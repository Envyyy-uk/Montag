"""Перевірка середовища: FFmpeg у PATH, NVENC, CUDA.

NVENC перевіряється пробним кодуванням одного кадру — наявність енкодера
в `ffmpeg -encoders` ще не означає, що драйвер і GPU його підтримують.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    required: bool = True


@dataclass
class SystemReport:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks if c.required)

    @property
    def nvenc(self) -> bool:
        return any(c.name == "nvenc" and c.ok for c in self.checks)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "nvenc": self.nvenc, "checks": [asdict(c) for c in self.checks]}


def _run(cmd: list[str], timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace")


def check_binary(name: str) -> CheckResult:
    path = shutil.which(name)
    if not path:
        return CheckResult(name, False, f"{name} не знайдено в PATH")
    try:
        out = _run([path, "-version"]).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult(name, False, f"{name} не запускається: {exc}")
    first = out.splitlines()[0] if out else ""
    m = re.search(r"version\s+(\S+)", first)
    return CheckResult(name, True, f"{m.group(1) if m else 'невідома версія'} ({path})")


def check_nvenc(ffmpeg: str = "ffmpeg") -> CheckResult:
    if not shutil.which(ffmpeg):
        return CheckResult("nvenc", False, "FFmpeg відсутній", required=False)
    try:
        encoders = _run([ffmpeg, "-hide_banner", "-encoders"]).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult("nvenc", False, f"не вдалося отримати список енкодерів: {exc}", required=False)
    if "h264_nvenc" not in encoders:
        return CheckResult("nvenc", False, "FFmpeg зібраний без h264_nvenc — буде CPU (libx264)",
                           required=False)
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
           "-i", "color=c=black:s=256x256:d=0.1", "-frames:v", "1",
           "-c:v", "h264_nvenc", "-f", "null", "-"]
    try:
        proc = _run(cmd)
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult("nvenc", False, f"пробне кодування не вдалося: {exc}", required=False)
    if proc.returncode != 0:
        err = (proc.stderr.strip().splitlines() or ["невідома помилка"])[0]
        return CheckResult("nvenc", False, f"h264_nvenc є, але не працює: {err}", required=False)
    hevc = "hevc_nvenc" in encoders
    return CheckResult("nvenc", True, "h264_nvenc працює" + (", hevc_nvenc є" if hevc else ""),
                       required=False)


def check_cuda() -> CheckResult:
    smi = shutil.which("nvidia-smi")
    if not smi:
        return CheckResult("cuda", False, "nvidia-smi не знайдено — whisper працюватиме на CPU",
                           required=False)
    try:
        proc = _run([smi, "--query-gpu=name,driver_version,memory.total",
                     "--format=csv,noheader"])
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult("cuda", False, f"nvidia-smi не запускається: {exc}", required=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        return CheckResult("cuda", False, "GPU NVIDIA не знайдено", required=False)
    return CheckResult("cuda", True, proc.stdout.strip().splitlines()[0], required=False)


def run_all() -> SystemReport:
    return SystemReport([
        check_binary("ffmpeg"),
        check_binary("ffprobe"),
        check_nvenc(),
        check_cuda(),
    ])


def format_report(report: SystemReport) -> str:
    lines = []
    for c in report.checks:
        mark = "OK " if c.ok else ("ПОМИЛКА" if c.required else "увага")
        lines.append(f"[{mark}] {c.name}: {c.detail}")
    lines.append("Система готова." if report.ok else "Є критичні проблеми — див. вище.")
    return "\n".join(lines)
