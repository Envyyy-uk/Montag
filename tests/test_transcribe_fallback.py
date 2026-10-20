"""Whisper: якщо CUDA «є», але бібліотек cuBLAS немає (типово на Windows) — перехід на CPU."""

import numpy as np

from analysis import transcribe as tr


class _Info:
    language, language_probability = "uk", 0.9


class _Model:
    def __init__(self, device):
        self.device = device

    def transcribe(self, audio, **kw):
        if self.device == "cuda":
            def boom():
                raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
                yield
            return boom(), _Info()
        return iter([]), _Info()


def test_falls_back_to_cpu_when_cuda_libs_missing(monkeypatch):
    monkeypatch.setattr(tr, "_cuda_broken", False)
    monkeypatch.setattr(tr, "_device", lambda: ("cuda", "float16"))
    monkeypatch.setattr(tr, "get_model", lambda name: _Model("cpu" if tr._cuda_broken else "cuda"))
    out = tr.transcribe(np.zeros(16000, dtype=np.float32))
    assert out["language"] == "uk" and tr._cuda_broken is True
