"""
Audio pause analyzer — core pipeline.

Flow: AAF → walk edited sequence → MXF clip ranges → ffmpeg audio extraction
      → reconstruct sequence audio → librosa pause detection → timecode strings.

Public API
----------
analyze(aaf_path, threshold_sec, on_progress) -> list[str]
    Full pipeline. Each returned string is one detected pause in the form
    "HH:MM:SS.mmm – HH:MM:SS.mmm  (N.NN s)".
    Raises AnalysisError on unrecoverable input problems.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
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


@dataclass
class _ClipRef:
    """A resolved reference to a range of audio inside an MXF file."""
    mxf_path: Path
    start_sec: float
    duration_sec: float


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
    try:
        y, sr, fps = _build_sequence_audio(aaf_path, _progress)
    except AnalysisError:
        raise
    except Exception as exc:
        raise AnalysisError(f"Nie można przetworzyć pliku AAF: {exc}") from exc

    _progress("Wykrywam pauzy w sekwencji…")
    pauses = _detect_pauses(y, sr, threshold_sec)
    return _format_pauses(pauses, fps=fps)


# ---------------------------------------------------------------------------
# AAF sequence traversal
# ---------------------------------------------------------------------------

def _build_sequence_audio(
    aaf_path: Path,
    on_progress: Callable[[str], None],
) -> tuple[np.ndarray, int, float]:
    """
    Parse the AAF, walk the edited sequence, extract only the used clip ranges
    from MXF files, and return (mixed_audio, sample_rate, fps).

    fps is the sequence edit rate (e.g. 25.0) used for frame timecode formatting.
    Fillers (gaps between clips) become silence at the correct duration.
    """
    with aaf2.open(str(aaf_path)) as f:
        mob_by_id: dict[str, object] = {str(mob.mob_id): mob for mob in f.content.mobs}

        main_comp = _find_main_composition(mob_by_id)
        if main_comp is None:
            raise AnalysisError(
                "Nie znaleziono sekwencji (CompositionMob) w pliku AAF.\n"
                "Upewnij się, że plik AAF zawiera zmontowaną sekwencję."
            )

        edit_rate = _get_slot_edit_rate(main_comp)
        audio_tracks: list[np.ndarray] = []

        slots = list(main_comp.slots)
        for slot in slots:
            seg = slot.segment
            if type(seg).__name__ not in ("Sequence", "SourceClip"):
                continue
            components = _components_of(seg)
            track_audio = _build_track_audio(
                components, mob_by_id, edit_rate, on_progress
            )
            if track_audio is not None:
                audio_tracks.append(track_audio)

    if not audio_tracks:
        raise AnalysisError(
            "Nie znaleziono ścieżek audio w sekwencji AAF.\n"
            "Sprawdź czy sekwencja zawiera zmontowane klipy audio."
        )

    # Mix all audio tracks (average) into one signal.
    max_len = max(len(t) for t in audio_tracks)
    mixed = np.zeros(max_len, dtype=np.float32)
    for track in audio_tracks:
        if len(track) < max_len:
            track = np.pad(track, (0, max_len - len(track)))
        mixed += track
    mixed /= len(audio_tracks)
    return mixed, _TARGET_SR, edit_rate


def _find_main_composition(mob_by_id: dict) -> object | None:
    """
    Return the top-level CompositionMob — the one not referenced as a source
    by any SourceClip in other mobs.
    """
    referenced_ids: set[str] = set()
    for mob in mob_by_id.values():
        for slot in getattr(mob, "slots", []):
            seg = getattr(slot, "segment", None)
            if seg is None:
                continue
            for comp in _components_of(seg):
                cp = _props(comp)
                src_id = str(cp.get("SourceID", ""))
                if src_id:
                    referenced_ids.add(src_id)

    for mob in mob_by_id.values():
        if type(mob).__name__ == "CompositionMob":
            if str(getattr(mob, "mob_id", "")) not in referenced_ids:
                return mob
    # Fallback: first CompositionMob
    for mob in mob_by_id.values():
        if type(mob).__name__ == "CompositionMob":
            return mob
    return None


def _get_slot_edit_rate(mob: object) -> float:
    """Return the edit rate (fps) from the first slot that has one."""
    for slot in getattr(mob, "slots", []):
        sp = _props(slot)
        rate = sp.get("EditRate")
        if rate is not None:
            # AAFRational or numeric
            try:
                return float(rate.numerator) / float(rate.denominator)
            except AttributeError:
                return float(rate)
    return 25.0  # Avid default


def _components_of(seg: object) -> list[object]:
    """Return the component list for a Sequence, or [seg] for a leaf node."""
    if type(seg).__name__ == "Sequence":
        try:
            return list(seg.components)
        except Exception:
            return []
    return [seg]


def _props(obj: object) -> dict:
    """Return a {name: value} dict of all AAF properties on obj."""
    try:
        return {p.name: p.value for p in obj.properties()}  # type: ignore[attr-defined]
    except Exception:
        return {}


def _build_track_audio(
    components: list[object],
    mob_by_id: dict,
    edit_rate: float,
    on_progress: Callable[[str], None],
) -> np.ndarray | None:
    """
    Build a numpy audio array for one timeline slot.

    Iterates the sequence components:
    - SourceClip → resolve to MXF clip range, extract audio
    - Filler → silence of the appropriate duration

    Returns None if no audio clips were found in this slot.
    """
    chunks: list[np.ndarray] = []
    has_audio = False

    for comp in components:
        cp = _props(comp)
        comp_type = type(comp).__name__
        length_frames = cp.get("Length", 0)

        if comp_type == "Filler":
            silence_samples = int(length_frames / edit_rate * _TARGET_SR)
            if silence_samples > 0:
                chunks.append(np.zeros(silence_samples, dtype=np.float32))

        elif comp_type == "SourceClip":
            src_id = str(cp.get("SourceID", ""))
            src_slot = cp.get("SourceMobSlotID")
            src_start = cp.get("StartTime", 0)
            if not src_id or src_slot is None:
                continue
            clip_refs = _resolve_clip(mob_by_id, src_id, src_slot, src_start, length_frames)
            for ref in clip_refs:
                real_path = ref.mxf_path
                if not real_path.exists():
                    found = _find_mxf_on_any_drive(real_path.name)
                    if found is None:
                        raise AnalysisError(
                            f"Nie znaleziono pliku mediów:\n{real_path.name}\n\n"
                            "Sprawdzono wszystkie dyski w folderach Avid MediaFiles\\MXF.\n"
                            "Upewnij się, że dysk z materiałem jest podłączony."
                        )
                    real_path = found
                on_progress(f"Ekstrahuję klip: {real_path.name} [{ref.start_sec:.1f}s +{ref.duration_sec:.1f}s]…")
                audio = _extract_clip_audio(real_path, ref.start_sec, ref.duration_sec)
                if audio.size > 0:
                    chunks.append(audio)
                    has_audio = True

    if not has_audio:
        return None
    return np.concatenate(chunks) if chunks else None


def _resolve_clip(
    mob_by_id: dict,
    mob_id: str,
    slot_id: int,
    start_frames: int,
    length_frames: int,
    _depth: int = 0,
) -> list[_ClipRef]:
    """
    Recursively follow the AAF reference chain from a SourceClip all the way
    to a SourceMob with PCMDescriptor, accumulating the start offset.

    Returns a list of _ClipRef — usually one item, but a Sequence in an
    intermediate mob can contribute multiple clips.
    """
    if _depth > 8 or not mob_id:
        return []

    mob = mob_by_id.get(mob_id)
    if mob is None:
        return []

    mob_type = type(mob).__name__

    # Leaf: SourceMob with audio essence
    if mob_type == "SourceMob":
        desc = getattr(mob, "descriptor", None)
        if desc is None or type(desc).__name__ != "PCMDescriptor":
            return []
        try:
            locator_list = desc["Locator"].value
        except (KeyError, AttributeError, TypeError):
            return []
        for locator in locator_list:
            url = _locator_url(locator)
            if not url:
                continue
            p = _url_to_path(url)
            if p and p.suffix.lower() == ".mxf":
                edit_rate = 25.0  # derive from descriptor if needed
                # PCMDescriptor has AudioSamplingRate but positions in the chain
                # are in the parent's edit_rate units; we accumulate in frames.
                return [_ClipRef(
                    mxf_path=p,
                    start_sec=start_frames / edit_rate,
                    duration_sec=length_frames / edit_rate,
                )]
        return []

    # Find the target slot in this mob
    target_slot = None
    for slot in getattr(mob, "slots", []):
        sp = _props(slot)
        if sp.get("SlotID") == slot_id:
            target_slot = slot
            break
    if target_slot is None:
        return []

    seg = getattr(target_slot, "segment", None)
    if seg is None:
        return []

    seg_type = type(seg).__name__

    if seg_type == "SourceClip":
        cp = _props(seg)
        next_mob_id = str(cp.get("SourceID", ""))
        next_slot_id = cp.get("SourceMobSlotID")
        next_start = cp.get("StartTime", 0)
        return _resolve_clip(
            mob_by_id, next_mob_id, next_slot_id,
            start_frames + next_start, length_frames, _depth + 1,
        )

    if seg_type == "Sequence":
        # Walk the sequence, find components that overlap [start_frames, start_frames+length_frames].
        components = _components_of(seg)
        pos = 0
        results: list[_ClipRef] = []
        for comp in components:
            cp = _props(comp)
            comp_len = cp.get("Length", 0)
            comp_end = pos + comp_len

            if type(comp).__name__ == "SourceClip":
                overlap_start = max(start_frames, pos)
                overlap_end = min(start_frames + length_frames, comp_end)
                if overlap_end > overlap_start:
                    offset_in_comp = overlap_start - pos
                    next_mob_id = str(cp.get("SourceID", ""))
                    next_slot_id = cp.get("SourceMobSlotID")
                    next_start = cp.get("StartTime", 0)
                    sub = _resolve_clip(
                        mob_by_id, next_mob_id, next_slot_id,
                        next_start + offset_in_comp, overlap_end - overlap_start,
                        _depth + 1,
                    )
                    results.extend(sub)
            pos = comp_end
        return results

    return []


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
# Audio extraction
# ---------------------------------------------------------------------------

def _extract_clip_audio(mxf_path: Path, start_sec: float, duration_sec: float) -> np.ndarray:
    """
    Extract a specific time range from an MXF file using bundled ffmpeg.

    Returns float32 mono samples at _TARGET_SR.
    Avid MXF uses uncompressed PCM — ffmpeg just demuxes it (fast).
    """
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [
        ffmpeg_exe,
        "-ss", str(start_sec),        # seek to start (before -i = fast seek)
        "-i", str(mxf_path),
        "-t", str(duration_sec),       # duration to extract
        "-vn",                         # skip video stream
        "-ac", "1",                    # mix down to mono
        "-ar", str(_TARGET_SR),        # resample to target rate
        "-f", "f32le",                 # raw 32-bit float PCM, little-endian
        "pipe:1",                      # write to stdout
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        stderr = proc.stderr.decode(errors="replace")
        raise AnalysisError(
            f"ffmpeg nie mógł odczytać pliku: {mxf_path.name}\n\n"
            f"Szczegóły ffmpeg:\n{stderr[-600:]}"
        )
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


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

def _format_pauses(pauses: list[tuple[float, float]], fps: float = 0.0) -> list[str]:
    """
    Format pause tuples as human-readable strings.

    When fps > 0 (sequence frame rate known), each line shows both the frame-based
    timecode (HH:MM:SS:FF — the format editors see in Avid) and the millisecond
    timecode, so either can be used for navigation.

    Example with fps=25:
        00:01:23:11 – 00:01:26:19  (3.33 s)   [00:01:23.456 – 00:01:26.789]

    Example without fps:
        00:01:23.456 – 00:01:26.789  (3.33 s)
    """
    lines = []
    for start, end in pauses:
        dur = end - start
        if fps > 0:
            tc_start = _to_tc_frames(start, fps)
            tc_end = _to_tc_frames(end, fps)
            ms_start = _to_tc(start)
            ms_end = _to_tc(end)
            lines.append(
                f"{tc_start} – {tc_end}  ({dur:.2f} s)"
                f"   [{ms_start} – {ms_end}]"
            )
        else:
            lines.append(f"{_to_tc(start)} – {_to_tc(end)}  ({dur:.2f} s)")
    return lines


def _to_tc(seconds: float) -> str:
    """Convert seconds to HH:MM:SS.mmm."""
    total_ms = int(round(seconds * 1000))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    m = (total_s // 60) % 60
    h = total_s // 3600
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _to_tc_frames(seconds: float, fps: float) -> str:
    """
    Convert seconds to broadcast frame timecode HH:MM:SS:FF.

    Uses the same integer-frame rounding as Avid Media Composer —
    frame number = floor(seconds * fps) % fps.
    """
    total_frames = int(seconds * fps)
    ff = total_frames % int(fps)
    total_s = total_frames // int(fps)
    s = total_s % 60
    m = (total_s // 60) % 60
    h = total_s // 3600
    return f"{h:02d}:{m:02d}:{s:02d}:{ff:02d}"
