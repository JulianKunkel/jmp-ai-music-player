"""Waveform and spectrum data for the window's visualizer.

The page draws a canvas; the expensive part — decoding audio — happens here
once per track and is cached in

    library-work/app/chat/waveforms/<hash>.json    peaks, 180 buckets
    library-work/app/chat/spectra/<hash>.json      24 FFT bands per 0.25 s

The key is ``rel_path`` plus file size and mtime, so a retagged or replaced file
is measured again.  ffmpeg decodes a mono 8 kHz stream, which is plenty for a
180-bucket envelope and cheap enough to do while a track plays.

The visualizer is decorative: a missing file, an SID/SPC ffmpeg cannot decode,
or a missing ffmpeg all return an empty peak list with an ``error`` instead of
raising.  Nothing here writes a music file or a tag.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from .catalog import AUDIO_EXT

RATE = 8000
BUCKETS = 180
MAX_SECONDS = 300
TIMEOUT = 25

#: How the page may draw the current track.  The first five share the cached
#: peak envelope, so switching between them costs no decoding; `spectrum` uses
#: the cached FFT bands and `axes` the stored sound profile.
STYLES = {
    "wave": "bars",
    "eq": "equalizer",
    "leds": "LED columns",
    "scope": "oscilloscope",
    "vu": "VU meters",
    "spectrum": "live spectrum (FFT)",
    "axes": "sound properties",
}
DEFAULT_STYLE = "wave"


def style(value, default: str = DEFAULT_STYLE) -> str:
    """Coerce a setting to a known visualizer style."""
    name = str(value or "").strip().casefold()
    return name if name in STYLES else default

try:                                    # numpy is in requirements; stay usable without it
    import numpy as _np
except Exception:                       # pragma: no cover - fallback path
    _np = None


def cache_dir(root: Path) -> Path:
    return Path(root) / "library-work" / "app" / "chat" / "waveforms"


def _safe_rel(root: Path, rel: str) -> Path | None:
    rel = str(rel or "").strip()
    if not rel or rel.startswith("/") or ".." in Path(rel).parts:
        return None
    path = Path(root) / rel
    try:
        path.resolve().relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return None
    if path.suffix.lower() not in AUDIO_EXT or not path.is_file():
        return None
    return path


def _cache_path(root: Path, rel: str, path: Path) -> Path:
    try:
        stat = path.stat()
        stamp = f"{rel}|{stat.st_size}|{int(stat.st_mtime)}|{RATE}|{BUCKETS}"
    except OSError:
        stamp = f"{rel}|?"
    digest = hashlib.sha1(stamp.encode("utf-8")).hexdigest()[:20]
    return cache_dir(root) / f"{digest}.json"


def _peaks(raw: bytes, buckets: int) -> list[float]:
    if not raw:
        return []
    if _np is not None:
        samples = _np.frombuffer(raw, dtype="<i2").astype(_np.float32)
        if samples.size == 0:
            return []
        chunk = max(1, samples.size // buckets)
        trimmed = samples[:chunk * buckets]
        if trimmed.size < buckets:
            return []
        grid = trimmed.reshape(buckets, chunk)
        envelope = _np.abs(grid).max(axis=1)
        scale = float(_np.percentile(envelope, 98)) or float(envelope.max()) or 1.0
        return [round(float(value), 4) for value in
                _np.clip(envelope / scale, 0.0, 1.0)]

    # no numpy: stdlib only, still fast enough for a cached value
    from array import array
    values = array("h")
    values.frombytes(raw[: len(raw) // 2 * 2])
    if not values:
        return []
    chunk = max(1, len(values) // buckets)
    peak = [0] * buckets
    step = max(1, chunk // 400)        # sample the window instead of every value
    for index in range(buckets):
        start = index * chunk
        window = values[start:start + chunk:step]
        peak[index] = max((abs(value) for value in window), default=0)
    scale = max(peak) or 1
    return [round(value / scale, 4) for value in peak]


def _read_wav_mono(path: Path) -> tuple[bytes, int]:
    """Decode to signed 16-bit mono PCM with ffmpeg."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not installed")
    argv = [ffmpeg, "-v", "error", "-nostdin", "-t", str(MAX_SECONDS),
            "-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", str(RATE),
            "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1"]
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=TIMEOUT)
    if result.returncode != 0 or not result.stdout:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else "could not decode audio")
    return result.stdout, RATE


def waveform(root: Path, rel: str, buckets: int = BUCKETS) -> dict:
    """Peaks in 0..1 for one track, cached on disk."""
    root = Path(root)
    path = _safe_rel(root, rel)
    if path is None:
        return {"rel": rel, "peaks": [], "buckets": 0,
                "error": "not an audio file inside the library"}
    cache = _cache_path(root, rel, path)
    data: dict = {}
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if data.get("peaks"):
        if buckets != len(data["peaks"]) and data.get("samples"):
            data["peaks"] = _resample(data["peaks"], buckets)
        data["rel"] = rel
        data["cached"] = True
        return data

    try:
        raw, rate = _read_wav_mono(path)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return {"rel": rel, "peaks": [], "buckets": 0,
                "error": str(exc)[:200]}
    peaks = _peaks(raw, BUCKETS)
    if not peaks:
        return {"rel": rel, "peaks": [], "buckets": 0,
                "error": "no decodable audio"}
    data = {
        "rel": rel,
        "peaks": peaks,
        "buckets": len(peaks),
        "seconds": round(len(raw) / 2 / rate, 2),
        "rate": rate,
        "measured": True,
    }
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, cache)
        _trim_cache(cache.parent)
    except OSError:
        pass
    data["cached"] = False
    if buckets != len(peaks):
        data["peaks"] = _resample(peaks, buckets)
    return data


# ---------------------------------------------------------------------------
# spectrum: a real FFT of the track, so the page can show the last second

SPECTRUM_RATE = 22050        # enough for 11 kHz, still cheap to decode
SPECTRUM_FRAME = 2048        # 93 ms at 22.05 kHz — a steady tone stays sharp
SPECTRUM_HOP = 0.25          # four frames per second, so "the last second"
SPECTRUM_BANDS = 24
SPECTRUM_LOW = 60.0
SPECTRUM_HIGH = 8000.0
SPECTRUM_RANGE = 45.0        # dB below the loudest band that still shows


def spectrum_dir(root: Path) -> Path:
    return Path(root) / "library-work" / "app" / "chat" / "spectra"


def _spectrum_cache(root: Path, rel: str, path: Path) -> Path:
    try:
        stat = path.stat()
        stamp = (f"{rel}|{stat.st_size}|{int(stat.st_mtime)}|"
                 f"{SPECTRUM_RATE}|{SPECTRUM_FRAME}|{SPECTRUM_HOP}|{SPECTRUM_BANDS}")
    except OSError:
        stamp = f"{rel}|?"
    digest = hashlib.sha1(stamp.encode("utf-8")).hexdigest()[:20]
    return spectrum_dir(root) / f"{digest}.json"


def _band_edges(bands: int) -> list[float]:
    high = min(SPECTRUM_HIGH, SPECTRUM_RATE / 2)
    if _np is None:
        return []
    return [round(float(value), 1) for value in
            _np.geomspace(SPECTRUM_LOW, high, bands + 1)]


def _bands(raw: bytes, rate: int, bands: int, hop: float) -> list[list[int]]:
    """Per-frame band energies, 0..100, from signed 16-bit mono PCM."""
    if _np is None or not raw:
        return []
    samples = _np.frombuffer(raw, dtype="<i2").astype(_np.float32)
    step = max(1, int(rate * hop))
    if samples.size < SPECTRUM_FRAME:
        return []
    count = 1 + (samples.size - SPECTRUM_FRAME) // step
    window = _np.hanning(SPECTRUM_FRAME).astype(_np.float32)
    freqs = _np.fft.rfftfreq(SPECTRUM_FRAME, 1.0 / rate)
    edges = _band_edges(bands)
    masks = [((freqs >= edges[index]) & (freqs < edges[index + 1]))
             for index in range(bands)]
    out = _np.empty((count, bands), dtype=_np.float32)
    # frame by frame in chunks: 1,200 x 2,048 at once would be 10 MB of float
    for start in range(0, count, 200):
        stop = min(count, start + 200)
        index = (_np.arange(SPECTRUM_FRAME)[None, :]
                 + step * _np.arange(start, stop)[:, None])
        block = _np.abs(_np.fft.rfft(samples[index] * window, axis=1))
        for band, mask in enumerate(masks):
            out[start:stop, band] = (block[:, mask].mean(axis=1)
                                     if mask.any() else 0.0)
    level = 20.0 * _np.log10(out + 1e-6)
    ceiling = float(_np.percentile(level, 99.5))
    scaled = _np.clip((level - (ceiling - SPECTRUM_RANGE)) / SPECTRUM_RANGE,
                      0.0, 1.0)
    return [[int(value) for value in row]
            for row in (scaled * 100).round().astype(int)]


def spectrum(root: Path, rel: str, bands: int = SPECTRUM_BANDS) -> dict:
    """FFT bands per frame for one track, cached on disk.

    ``frames`` holds one row of ``bands`` values (0..100) every
    :data:`SPECTRUM_HOP` seconds, so the page can show the spectrum of the
    second around the playhead.  Without numpy there is no spectrum — the
    answer says so instead of inventing one.
    """
    root = Path(root)
    path = _safe_rel(root, rel)
    if path is None:
        return {"rel": rel, "frames": [], "bands": 0,
                "error": "not an audio file inside the library"}
    if _np is None:
        return {"rel": rel, "frames": [], "bands": 0,
                "error": "numpy is not installed, so there is no FFT"}
    cache = _spectrum_cache(root, rel, path)
    data: dict = {}
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if data.get("frames"):
        data["rel"] = rel
        data["cached"] = True
        return data

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return {"rel": rel, "frames": [], "bands": 0,
                "error": "ffmpeg is not installed"}
    argv = [ffmpeg, "-v", "error", "-nostdin", "-t", str(MAX_SECONDS),
            "-i", str(path), "-map", "0:a:0", "-ac", "1",
            "-ar", str(SPECTRUM_RATE), "-f", "s16le", "-acodec", "pcm_s16le",
            "pipe:1"]
    try:
        result = subprocess.run(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=TIMEOUT + 20)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"rel": rel, "frames": [], "bands": 0, "error": str(exc)[:200]}
    if result.returncode != 0 or not result.stdout:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        return {"rel": rel, "frames": [], "bands": 0,
                "error": (detail[-1] if detail else "could not decode audio")[:200]}
    frames = _bands(result.stdout, SPECTRUM_RATE, bands, SPECTRUM_HOP)
    if not frames:
        return {"rel": rel, "frames": [], "bands": 0,
                "error": "no decodable audio"}
    data = {
        "rel": rel,
        "frames": frames,
        "bands": bands,
        "hop": SPECTRUM_HOP,
        "edges": _band_edges(bands),
        "rate": SPECTRUM_RATE,
        "seconds": round(len(result.stdout) / 2 / SPECTRUM_RATE, 2),
        "measured": True,
    }
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, cache)
        _trim_cache(cache.parent)
    except OSError:
        pass
    data["cached"] = False
    return data


def _resample(peaks: list[float], buckets: int) -> list[float]:
    if buckets <= 0 or not peaks:
        return []
    step = len(peaks) / buckets
    out = []
    for index in range(buckets):
        start = int(index * step)
        end = max(start + 1, int((index + 1) * step))
        window = peaks[start:end]
        out.append(round(max(window) if window else 0.0, 4))
    return out


def _trim_cache(folder: Path, keep: int = 500) -> None:
    try:
        files = sorted(folder.glob("*.json"),
                       key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return
    for stale in files[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass
