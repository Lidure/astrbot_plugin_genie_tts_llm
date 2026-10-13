"""Conservative acoustic repetition checks for Genie TTS WAV output.

This is NOT speech recognition. It detects near-identical *waveform* motifs
in one recording, and motifs reused across different TTS texts for the same
character. Similar wording with different acoustics may be missed, so use
these checks as an additional safeguard, not as a transcription guarantee.

NumPy is optional: on systems without it, no audio-content rejection occurs;
the existing duration guards remain available. No data leaves the process.
"""
from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

try:
    import numpy as _np
except ImportError:  # Optional lightweight signal-processing enhancement.
    _np = None

TARGET_RATE = 800  # downsampled comparisons keep Pi CPU load modest
WINDOW_SECONDS = 1.35
HOP_SECONDS = 0.30
MIN_SEPARATION_SECONDS = 2.5
MAX_ANALYZED_SECONDS = 75
MAX_SIGNATURE_SEGMENTS = 250
MIN_RMS = 0.014
REPEAT_CORRELATION_THRESHOLD = 0.985


@dataclass(slots=True)
class AudioSignature:
    windows: Any
    starts: Any
    sample_rate: int


def fingerprint_wav(file_path: str | Path) -> AudioSignature | None:
    """Extract normalized audio windows from a mono 16-bit PCM WAV.

    Return None for unsupported, silent or very long audio, rather than
    accidentally rejecting speech. Does not write or mutate the WAV.
    """
    if _np is None:
        return None
    try:
        with wave.open(str(file_path), "rb") as wav:
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                return None
            src_rate = wav.getframerate()
            if src_rate < 8000 or src_rate > 96000:
                return None
            if wav.getnframes() > src_rate * MAX_ANALYZED_SECONDS:
                return None
            pcm = wav.readframes(wav.getnframes())
    except (OSError, ValueError, EOFError, wave.Error):
        return None

    step = max(1, round(src_rate / TARGET_RATE))
    rate = src_rate / step
    samples = _np.frombuffer(pcm, dtype="<i2")[::step].astype(_np.float32) / 32768.0
    window = round(WINDOW_SECONDS * rate)
    hop = max(1, round(HOP_SECONDS * rate))
    if samples.size < window * 2:
        return None
    starts = _np.arange(0, samples.size - window + 1, hop, dtype=_np.int32)
    if starts.size > MAX_SIGNATURE_SEGMENTS:
        starts = starts[:MAX_SIGNATURE_SEGMENTS]
    # Copy only a capped set of windows; no full spectrogram or FFT dependency.
    slices = starts[:, None] + _np.arange(window, dtype=_np.int32)[None, :]
    windows = samples[slices]
    rms = _np.sqrt(_np.mean(windows * windows, axis=1))
    valid = rms >= MIN_RMS
    if not valid.any():
        return None
    windows = windows[valid]
    starts = starts[valid].astype(_np.float32) / rate
    windows -= windows.mean(axis=1, keepdims=True)
    norms = _np.linalg.norm(windows, axis=1)
    keep = norms > 1e-4
    if not keep.any():
        return None
    windows = windows[keep] / norms[keep, None]
    starts = starts[keep]
    return AudioSignature(windows=windows, starts=starts, sample_rate=round(rate))


def repeated_within(signature: AudioSignature) -> bool:
    """Find a near-identical >=1.35s motif repeated at least 2.5s later."""
    if _np is None or signature.windows.shape[0] < 2:
        return False
    correlations = signature.windows @ signature.windows.T
    separations = _np.abs(
        signature.starts[:, None] - signature.starts[None, :]
    )
    matches = (
        (correlations >= REPEAT_CORRELATION_THRESHOLD)
        & (separations >= MIN_SEPARATION_SECONDS)
    )
    return bool(matches.any())


def repeated_across(
    signature: AudioSignature, previous: Sequence[AudioSignature]
) -> bool:
    """Detect same waveform motif across distinct completed utterances."""
    if _np is None:
        return False
    for older in previous:
        if older.windows.shape[1] != signature.windows.shape[1]:
            continue
        if bool(
            (signature.windows @ older.windows.T
             >= REPEAT_CORRELATION_THRESHOLD).any()
        ):
            return True
    return False


def classify_audio_repeat(
    path: str | Path, previous: Sequence[AudioSignature] = ()
) -> tuple[str | None, AudioSignature | None]:
    """Return ('internal' | 'cross' | None, signature) for a WAV."""
    signature = fingerprint_wav(path)
    if signature is None:
        return None, None
    if repeated_within(signature):
        return "internal", signature
    if previous and repeated_across(signature, previous):
        return "cross", signature
    return None, signature
