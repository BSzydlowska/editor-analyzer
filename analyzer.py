"""
Audio pause analyzer — core pipeline.

Flow: AAF → MXF paths → ffmpeg audio extraction → librosa pause detection → timecode strings.

Public API
----------
analyze(aaf_path, threshold_sec, on_progress) -> list[str]
    Full pipeline. Each returned string is one detected pause in the form
    "HH:MM:SS.mmm – HH:MM:SS.mmm  (N.NN s)".
    Raises AnalysisError on unrecoverable input problems.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable
from urllib.parse import unquote

import aaf2
import imageio_ffmpeg
import librosa
import numpy as np

# Extract audio at 16 kHz mono — sufficient for pause detection (~0.5 s resolution),
# ~14× faster to process than 44.1 kHz stereo.
_TARGET_SR = 16_000

# Segments more than this many dB below the peak are treated as silent by librosa.
_TOP_DB = 40


class AnalysisError(Exception):
    """Raised when analysis cannot proceed (bad file, missing media, etc.)."""


def analyze(
    aaf_path: Path,
    threshold_sec: float,
    on_progress: Callable[[str], None] | None = None,
) -> list[str]:
    """
    Run the full pipeline and return formatted pause strings.

    Parameters
    ----------
    aaf_path:       Path to the .aaf project file.
    threshold_sec:  Minimum pause duration in seconds to report.
    on_progress:    Optional callback called with a status string at each step.
                    Must be thread-safe (the UI calls self.after() inside it).
    """

    def _progress(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    _progress("Wczytuję plik AAF…")
    mxf_paths = _resolve_mxf_paths(aaf_path)
    if not mxf_paths:
        raise AnalysisError(
            "Nie znaleziono referencji do plików MXF w pliku AAF.\n"
            "Upewnij się, że plik AAF wskazuje na lokalne pliki mediów."
        )

    results: list[str] = []
    for i, mxf_path in enumerate(mxf_paths, 1):
        _progress(f"Ekstrahuję audio ({i}/{len(mxf_paths)}): {mxf_path.name}…")
        # If the recorded drive is unavailable, scan Avid MediaFiles on all drives.
        if not mxf_path.exists():
            found = _find_mxf_on_any_drive(mxf_path.name)
            if found is None:
                raise AnalysisError(
                    f"Nie znaleziono pliku mediów:\n{mxf_path.name}\n\n"
                    "Sprawdzono wszystkie dyski w folderach Avid MediaFiles\\MXF.\n"
                    "Upewnij się, że dysk z materiałem jest podłączony."
                )
            mxf_path = found
        y, sr = _extract_audio(mxf_path)

        _progress(f"Wykrywam pauzy ({i}/{len(mxf_paths)}): {mxf_path.name}…")
        pauses = _detect_pauses(y, sr, threshold_sec)
        results.extend(_format_pauses(pauses))

    return results


# ---------------------------------------------------------------------------
# AAF parsing
# ---------------------------------------------------------------------------

def _resolve_mxf_paths(aaf_path: Path) -> list[Path]:
    """Parse AAF and return a deduplicated ordered list of referenced MXF audio paths.

    Avid AAF locators are accessed via descriptor['Locator'].value (not descriptor.locators).
    If the recorded drive letter is unavailable (e.g. media moved from F: to C:),
    falls back to scanning all Avid MediaFiles folders on connected drives.
    Only audio descriptors (PCMDescriptor) are processed; video (CDCIDescriptor)
    and original-source references (ImportDescriptor) are skipped.
    """
    paths: list[Path] = []
    try:
        with aaf2.open(str(aaf_path)) as f:
            for mob in f.content.mobs:
                desc = getattr(mob, "descriptor", None)
                if desc is None:
                    continue
                # Skip video and original-source descriptors — only want Avid MXF audio
                if type(desc).__name__ != "PCMDescriptor":
                    continue
                try:
                    locator_list = desc["Locator"].value
                except (KeyError, AttributeError, TypeError):
                    continue
                for locator in locator_list:
                    url = _locator_url(locator)
                    if not url:
                        continue
                    p = _url_to_path(url)
                    if p and p.suffix.lower() == ".mxf":
                        paths.append(p)
    except AnalysisError:
        raise
    except Exception as exc:
        raise AnalysisError(f"Nie można odczytać pliku AAF: {exc}") from exc

    # Deduplicate preserving insertion order.
    seen: set[Path] = set()
    unique: list[Path] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def _locator_url(locator: object) -> str:
    """Extract URL string from an aaf2 locator object, trying both property names."""
    for key in ("URLString", "FilePath"):
        try:
            value = locator[key].value  # type: ignore[index]
            if isinstance(value, str) and value.strip():
                return value.strip()
        except (KeyError, AttributeError, TypeError):
            continue
    return ""


def _url_to_path(url: str) -> Path | None:
    """
    Convert a file:// URL or raw Windows path from an AAF locator to a Path.

    Avid typically writes absolute Windows paths as file:///C:/path/to/reel.mxf.
    """
    url = url.strip()
    if url.startswith("file:///"):
        return Path(unquote(url[8:]))
    if url.startswith("file://"):
        return Path(unquote(url[7:]))
    if url:
        return Path(url)
    return None


def _find_mxf_on_any_drive(filename: str) -> Path | None:
    """
    Fallback: search for *filename* inside Avid MediaFiles\\MXF\\ on every
    connected Windows drive (C:, D:, E: … Z:).

    Avid always stores MXF files under <drive>\\Avid MediaFiles\\MXF\\<project>\\.
    When media was recorded on a different machine or drive letter, the locator
    path in the AAF will be wrong but the filename is still unique and stable.
    """
    import string
    for letter in string.ascii_uppercase:
        root = Path(f"{letter}:\\Avid MediaFiles\\MXF")
        if not root.is_dir():
            continue
        # Walk one level deep (project subfolders)
        for project_dir in root.iterdir():
            if not project_dir.is_dir():
                continue
            candidate = project_dir / filename
            if candidate.exists():
                return candidate
    return None



# ---------------------------------------------------------------------------

def _extract_audio(mxf_path: Path) -> tuple[np.ndarray, int]:
    """
    Use the bundled ffmpeg (imageio-ffmpeg) to read audio from a MXF file.

    Returns (samples, sample_rate) where samples is a float32 numpy array.
    Avid MXF uses uncompressed PCM, so ffmpeg essentially just demuxes —
    a 1-hour reel typically extracts in under 15 seconds.
    """
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [
        ffmpeg_exe,
        "-i", str(mxf_path),
        "-vn",                    # skip video stream
        "-ac", "1",               # mix down to mono
        "-ar", str(_TARGET_SR),   # resample to target rate
        "-f", "f32le",            # raw 32-bit float PCM, little-endian
        "pipe:1",                 # write to stdout
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        stderr = proc.stderr.decode(errors="replace")
        raise AnalysisError(
            f"ffmpeg nie mógł odczytać pliku: {mxf_path.name}\n\n"
            f"Szczegóły ffmpeg:\n{stderr[-600:]}"
        )
    y = np.frombuffer(proc.stdout, dtype=np.float32).copy()
    return y, _TARGET_SR


# ---------------------------------------------------------------------------
# Pause detection
# ---------------------------------------------------------------------------

def _detect_pauses(
    y: np.ndarray,
    sr: int,
    threshold_sec: float,
) -> list[tuple[float, float]]:
    """
    Detect silent gaps in y longer than threshold_sec.

    Uses librosa.effects.split() which computes RMS energy per frame and
    marks frames more than _TOP_DB dB below the signal peak as silent.
    This captures quiet breathing and low hum — not just hard silence —
    matching the energy-level rule in the PRD.

    Returns a list of (start_sec, end_sec) tuples.
    """
    if y.size == 0:
        return []

    intervals = librosa.effects.split(y, top_db=_TOP_DB, frame_length=2048, hop_length=512)
    total_samples = len(y)
    pauses: list[tuple[float, float]] = []

    prev_end = 0
    for start, end in intervals:
        gap_start = prev_end / sr
        gap_end = start / sr
        if gap_end - gap_start >= threshold_sec:
            pauses.append((gap_start, gap_end))
        prev_end = end

    # Check trailing silence after the last non-silent interval.
    gap_start = prev_end / sr
    gap_end = total_samples / sr
    if gap_end - gap_start >= threshold_sec:
        pauses.append((gap_start, gap_end))

    return pauses


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _format_pauses(pauses: list[tuple[float, float]]) -> list[str]:
    return [
        f"{_to_tc(start)} – {_to_tc(end)}  ({end - start:.2f} s)"
        for start, end in pauses
    ]


def _to_tc(seconds: float) -> str:
    """Convert seconds to HH:MM:SS.mmm."""
    total_ms = int(round(seconds * 1000))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    m = (total_s // 60) % 60
    h = total_s // 3600
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"
