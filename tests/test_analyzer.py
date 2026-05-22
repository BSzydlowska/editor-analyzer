"""Unit tests for analyzer.py — no network, no real AAF/MXF files required."""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from analyzer import (
    AnalysisError,
    _detect_pauses,
    _format_pauses,
    _to_tc,
    _to_tc_frames,
    _url_to_path,
)


# ---------------------------------------------------------------------------
# _to_tc — timecode formatting
# ---------------------------------------------------------------------------

class TestToTc:
    def test_zero(self) -> None:
        assert _to_tc(0.0) == "00:00:00.000"

    def test_one_second(self) -> None:
        assert _to_tc(1.0) == "00:00:01.000"

    def test_one_minute(self) -> None:
        assert _to_tc(60.0) == "00:01:00.000"

    def test_one_hour(self) -> None:
        assert _to_tc(3600.0) == "01:00:00.000"

    def test_mixed(self) -> None:
        # 1h 2m 3s 456ms
        assert _to_tc(3600 + 120 + 3 + 0.456) == "01:02:03.456"

    def test_milliseconds(self) -> None:
        assert _to_tc(0.001) == "00:00:00.001"
        assert _to_tc(0.999) == "00:00:00.999"

    def test_large_hours(self) -> None:
        # 20 hours of footage
        assert _to_tc(20 * 3600.0) == "20:00:00.000"


# ---------------------------------------------------------------------------
# _url_to_path — AAF locator URL → Path
# ---------------------------------------------------------------------------

class TestUrlToPath:
    def test_file_triple_slash_windows(self) -> None:
        p = _url_to_path("file:///C:/media/reel1.mxf")
        assert p == Path("C:/media/reel1.mxf")

    def test_file_double_slash(self) -> None:
        p = _url_to_path("file://C:/media/reel2.mxf")
        assert p == Path("C:/media/reel2.mxf")

    def test_raw_path(self) -> None:
        p = _url_to_path(r"C:\media\reel3.mxf")
        assert p == Path(r"C:\media\reel3.mxf")

    def test_empty_returns_none(self) -> None:
        assert _url_to_path("") is None
        assert _url_to_path("   ") is None

    def test_url_encoded_spaces(self) -> None:
        p = _url_to_path("file:///C:/my%20media/reel%201.mxf")
        assert p == Path("C:/my media/reel 1.mxf")


# ---------------------------------------------------------------------------
# _detect_pauses — pause detection on synthetic audio
# ---------------------------------------------------------------------------

SR = 16_000  # matches _TARGET_SR


def _make_signal(duration_sec: float, silent_intervals: list[tuple[float, float]]) -> np.ndarray:
    """
    Build a float32 audio array with 0.9 amplitude everywhere, then zero out
    the specified silent intervals (start_sec, end_sec).
    """
    n = int(duration_sec * SR)
    y = np.full(n, 0.9, dtype=np.float32)
    for start, end in silent_intervals:
        s = int(start * SR)
        e = int(end * SR)
        y[s:e] = 0.0
    return y


class TestDetectPauses:
    def test_no_pauses(self) -> None:
        y = _make_signal(10.0, [])
        assert _detect_pauses(y, SR, threshold_sec=2.0) == []

    def test_single_pause_detected(self) -> None:
        # 5-second silence starting at t=3
        y = _make_signal(15.0, [(3.0, 8.0)])
        pauses = _detect_pauses(y, SR, threshold_sec=2.0)
        assert len(pauses) == 1
        start, end = pauses[0]
        assert math.isclose(start, 3.0, abs_tol=0.1)
        assert math.isclose(end, 8.0, abs_tol=0.1)

    def test_pause_below_threshold_not_detected(self) -> None:
        # 1-second silence, threshold = 2 s
        y = _make_signal(10.0, [(4.0, 5.0)])
        assert _detect_pauses(y, SR, threshold_sec=2.0) == []

    def test_multiple_pauses(self) -> None:
        y = _make_signal(30.0, [(2.0, 6.0), (15.0, 21.0)])
        pauses = _detect_pauses(y, SR, threshold_sec=2.0)
        assert len(pauses) == 2

    def test_empty_audio(self) -> None:
        assert _detect_pauses(np.array([], dtype=np.float32), SR, threshold_sec=2.0) == []

    def test_exact_threshold_boundary(self) -> None:
        # librosa.effects.split works in frames (hop_length=512 @ 16 kHz = 32 ms).
        # A 3.0 s synthetic silence is detected as ~2.88 s due to frame snapping.
        # Use threshold=2.5 s to stay comfortably above the quantization floor.
        y = _make_signal(10.0, [(3.0, 6.0)])  # 3.0 s pause, detected as ~2.88 s
        pauses = _detect_pauses(y, SR, threshold_sec=2.5)
        assert len(pauses) == 1


# ---------------------------------------------------------------------------
# _format_pauses — output string formatting
# ---------------------------------------------------------------------------

class TestFormatPauses:
    def test_format_single(self) -> None:
        lines = _format_pauses([(3.0, 7.5)])
        assert len(lines) == 1
        assert "00:00:03.000" in lines[0]
        assert "00:00:07.500" in lines[0]
        assert "4.50 s" in lines[0]

    def test_format_empty(self) -> None:
        assert _format_pauses([]) == []

    def test_format_multiple(self) -> None:
        pauses = [(0.5, 4.0), (60.0, 65.0)]
        lines = _format_pauses(pauses)
        assert len(lines) == 2
        assert "00:01:00.000" in lines[1]


# ---------------------------------------------------------------------------
# _to_tc_frames — frame timecode formatting
# ---------------------------------------------------------------------------

class TestToTcFrames:
    def test_zero(self) -> None:
        assert _to_tc_frames(0.0, 25.0) == "00:00:00:00"

    def test_one_second(self) -> None:
        assert _to_tc_frames(1.0, 25.0) == "00:00:01:00"

    def test_half_second_25fps(self) -> None:
        # 0.5 s * 25 fps = 12.5 → floor = 12 frames
        assert _to_tc_frames(0.5, 25.0) == "00:00:00:12"

    def test_one_minute(self) -> None:
        assert _to_tc_frames(60.0, 25.0) == "00:01:00:00"

    def test_one_hour(self) -> None:
        assert _to_tc_frames(3600.0, 25.0) == "01:00:00:00"

    def test_frame_wraps_at_fps(self) -> None:
        # 24 frames at 25fps = 0.96 s; next frame (25th) wraps to :01:00
        assert _to_tc_frames(0.96, 25.0) == "00:00:00:24"
        assert _to_tc_frames(1.0, 25.0) == "00:00:01:00"

    def test_30fps(self) -> None:
        assert _to_tc_frames(1.0, 30.0) == "00:00:01:00"
        assert _to_tc_frames(0.5, 30.0) == "00:00:00:15"


class TestFormatPausesWithFps:
    def test_shows_frame_tc_when_fps_given(self) -> None:
        lines = _format_pauses([(3.0, 7.5)], fps=25.0)
        assert len(lines) == 1
        # Frame TC: 3.0s → 00:00:03:00, 7.5s → 00:00:07:12
        assert "00:00:03:00" in lines[0]
        assert "00:00:07:12" in lines[0]
        # Millisecond TC still present in brackets
        assert "00:00:03.000" in lines[0]
        assert "00:00:07.500" in lines[0]
        assert "4.50 s" in lines[0]

    def test_no_frame_tc_when_fps_zero(self) -> None:
        lines = _format_pauses([(3.0, 7.5)], fps=0.0)
        assert ":" in lines[0]
        assert "00:00:03.000" in lines[0]
