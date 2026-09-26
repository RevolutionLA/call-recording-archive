"""Audio helpers: probe duration, normalize to 16k mono wav via ffmpeg."""
from __future__ import annotations
import subprocess, hashlib, json, os
from pathlib import Path
from typing import Optional


def _run(cmd: list, timeout: int = 600) -> subprocess.CompletedProcess:
    si = None
    if os.name == "nt":  # windows: hide console window
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, startupinfo=si)


def probe_duration(path: str | Path) -> Optional[float]:
    r = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
              "-of", "json", str(path)])
    try:
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return None


def normalize(path: str | Path, work_dir: str | Path) -> Path:
    """Convert any audio to 16kHz mono pcm wav cached under work_dir.

    Returns the wav path. Idempotent (skips existing valid file).
    """
    src = Path(path)
    wd = Path(work_dir)
    wd.mkdir(parents=True, exist_ok=True)
    h = hashlib.md5(str(src.resolve()).encode("utf-8")).hexdigest()[:12]
    out = wd / f"{src.stem[:40]}_{h}.wav".replace(" ", "_")
    if out.exists() and out.stat().st_size > 44:
        return out
    r = _run(["ffmpeg", "-y", "-v", "error", "-i", str(src),
              "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out)])
    if r.returncode != 0 or not out.exists():
        raise RuntimeError(f"ffmpeg failed for {src}: {r.stderr[-400:]}")
    return out


def load_wav(path: str | Path):
    """Read wav as float32 mono numpy array + sr (for voiceprint export)."""
    import wave, audioop
    import numpy as np
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        raw = w.readframes(n)
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if w.getnchannels() > 1:
        data = data.reshape(-1, w.getnchannels()).mean(axis=1).astype(np.float32)
    return data, sr


def slice_wav(src_wav: str | Path, dst_wav: str | Path, start_ms: int, end_ms: int,
              target_sr: int = 24000):
    """Cut [start,end] ms from a wav, resample to target_sr."""
    Path(dst_wav).parent.mkdir(parents=True, exist_ok=True)
    r = _run(["ffmpeg", "-y", "-v", "error", "-i", str(src_wav),
              "-ss", f"{start_ms/1000:.3f}", "-to", f"{end_ms/1000:.3f}",
              "-ar", str(target_sr), "-ac", "1", "-c:a", "pcm_s16le", str(dst_wav)])
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg slice failed: {r.stderr[-300:]}")
