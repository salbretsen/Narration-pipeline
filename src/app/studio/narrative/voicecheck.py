"""Acoustic voice-consistency measurement across independently generated segments.

Each segment is synthesised on its own, and a designed or cloned voice is not guaranteed to sound
the same every time. This module measures that drift with two simple, transparent features per
segment - median fundamental frequency (autocorrelation pitch tracker on voiced frames) and
spectral centroid - and reports their spread per voice. It describes *acoustic* similarity only:
it is not speaker verification and says nothing about emotional quality.
"""
from __future__ import annotations

import numpy as np


def _frames(x: np.ndarray, size: int, hop: int):
    for i in range(0, max(0, len(x) - size), hop):
        yield x[i:i + size]


def f0_median(audio: np.ndarray, sr: int, fmin: float = 70.0, fmax: float = 400.0) -> float | None:
    """Median pitch in Hz over voiced frames, or None when nothing is clearly voiced."""
    x = np.asarray(audio, dtype=np.float64)
    if x.size < sr // 4:
        return None
    size, hop = int(0.04 * sr), int(0.02 * sr)
    lo, hi = int(sr / fmax), int(sr / fmin)
    peak = float(np.max(np.abs(x))) or 1.0
    f0s = []
    for fr in _frames(x, size, hop):
        if np.sqrt(np.mean(fr ** 2)) < 0.05 * peak:
            continue
        fr = fr - np.mean(fr)
        ac = np.correlate(fr, fr, mode="full")[len(fr) - 1:]
        if ac[0] <= 0:
            continue
        ac = ac / ac[0]
        seg = ac[lo:hi]
        if seg.size == 0:
            continue
        k = int(np.argmax(seg))
        if seg[k] >= 0.45:
            f0s.append(sr / (lo + k))
    return float(np.median(f0s)) if len(f0s) >= 5 else None


def spectral_centroid(audio: np.ndarray, sr: int) -> float:
    x = np.asarray(audio, dtype=np.float64)
    if x.size == 0:
        return 0.0
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    freqs = np.fft.rfftfreq(len(x), 1.0 / sr)
    return float((spec * freqs).sum() / (spec.sum() or 1.0))


def consistency(items: list[tuple[str, np.ndarray, int]]) -> dict:
    """items = [(voice label, audio, sample rate)] -> per voice: segment count, median f0 mean/std/CV, centroid mean/CV."""
    by: dict[str, list[tuple[float | None, float]]] = {}
    for label, audio, sr in items:
        by.setdefault(label, []).append((f0_median(audio, sr), spectral_centroid(audio, sr)))
    out = {}
    for label, vals in by.items():
        f0 = np.array([v[0] for v in vals if v[0] is not None])
        cen = np.array([v[1] for v in vals])
        out[label] = {
            "segments": len(vals), "voiced_segments": int(f0.size),
            "f0_mean_hz": round(float(f0.mean()), 1) if f0.size else None,
            "f0_cv": round(float(f0.std() / f0.mean()), 3) if f0.size > 1 and f0.mean() else None,
            "centroid_mean_hz": round(float(cen.mean()), 1) if cen.size else None,
            "centroid_cv": round(float(cen.std() / cen.mean()), 3) if cen.size > 1 and cen.mean() else None,
        }
    return out
