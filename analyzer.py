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
# 40 dB is conservative — covers clear pauses without misclassifying soft speech.
_TOP_DB = 30

# Non-silent intervals closer than this (in seconds) are merged into one.
# Prevents soft transitions, breathing, or slight energy dips within speech from
# fragmenting genuine speech and creating fake short "pauses" between fragments.
_MERGE_GAP_SEC = 0.2


class AnalysisError(Exception):
    """Raised when analysis cannot proceed (bad file, missing media, etc.)."""


@dataclass
class AnalysisResult:
    """Return value of analyze() — pause lines plus summary statistics."""
    lines: list[str]
    total_sec: float        # analyzed sequence duration (without skipped segments)
    pause_sec: float        # sum of all detected pauses
    audio_sec: float        # analyzed speech/non-pause duration
    skipped_sec: float      # total skipped time (mapping/read errors)
    skipped_segments: list["SkippedSegment"]


@dataclass
class SkippedSegment:
    """A timeline segment that could not be analyzed, with source and reason."""
    file_name: str
    reason: str
    duration_sec: float


@dataclass
class _TrackBuildResult:
    """Build output for one timeline track."""
    audio: np.ndarray | None
    skipped_intervals: list[tuple[float, float]]
    skipped_segments: list[SkippedSegment]
    timeline_samples: int


@dataclass
class _ClipRef:
    """A resolved reference to a range of audio inside an MXF file."""
    mxf_path: Path
    start_sec: float
    duration_sec: float


def analyze(
    aaf_path: Path,
    threshold_sec: float,
    top_db: int = _TOP_DB,
    on_progress: Callable[[str], None] | None = None,
) -> AnalysisResult:
    """
    Run the full pipeline and return an AnalysisResult with pause lines and stats.

    Parameters
    ----------
    aaf_path:       Path to the .aaf project file.
    threshold_sec:  Minimum pause duration in seconds to report.
    top_db:         dB threshold for silence detection (librosa top_db).
                    Lower = less sensitive (only clear silences). Default 30.
    on_progress:    Optional callback called with a status string at each step.
                    Must be thread-safe (the UI calls self.after() inside it).
    """

    def _progress(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    _progress("Wczytuję plik AAF…")
    try:
        y, sr, fps, skipped_intervals, skipped_segments = _build_sequence_audio(aaf_path, _progress)
    except AnalysisError:
        raise
    except Exception as exc:
        raise AnalysisError(f"Nie można przetworzyć pliku AAF: {exc}") from exc

    _progress("Wykrywam pauzy w sekwencji…")
    pauses = _detect_pauses(y, sr, threshold_sec, top_db=top_db)
    merged_skipped = _merge_intervals(skipped_intervals)
    if merged_skipped:
        pauses = _subtract_intervals(pauses, merged_skipped, min_duration=threshold_sec)
    lines = _format_pauses(pauses, fps=fps)

    timeline_total_sec = len(y) / sr
    skipped_sec = sum(end - start for start, end in merged_skipped)
    total_sec = max(0.0, timeline_total_sec - skipped_sec)
    pause_sec = sum(end - start for start, end in pauses)
    grouped_skipped = _aggregate_skipped_segments(skipped_segments)
    return AnalysisResult(
        lines=lines,
        total_sec=total_sec,
        pause_sec=pause_sec,
        audio_sec=max(0.0, total_sec - pause_sec),
        skipped_sec=skipped_sec,
        skipped_segments=grouped_skipped,
    )


# ---------------------------------------------------------------------------
# AAF sequence traversal
# ---------------------------------------------------------------------------

def _build_sequence_audio(
    aaf_path: Path,
    on_progress: Callable[[str], None],
) -> tuple[np.ndarray, int, float, list[tuple[float, float]], list[SkippedSegment]]:
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
        all_skipped_intervals: list[tuple[float, float]] = []
        all_skipped_segments: list[SkippedSegment] = []
        max_timeline_samples = 0

        slots = list(main_comp.slots)
        for slot in slots:
            seg = slot.segment
            if type(seg).__name__ not in ("Sequence", "SourceClip"):
                continue
            components = _components_of(seg)
            track_result = _build_track_audio(
                components, mob_by_id, edit_rate, on_progress
            )
            max_timeline_samples = max(max_timeline_samples, track_result.timeline_samples)
            all_skipped_intervals.extend(track_result.skipped_intervals)
            all_skipped_segments.extend(track_result.skipped_segments)
            if track_result.audio is not None:
                audio_tracks.append(track_result.audio)

    if not audio_tracks and max_timeline_samples <= 0:
        raise AnalysisError(
            "Nie znaleziono ścieżek audio w sekwencji AAF.\n"
            "Sprawdź czy sekwencja zawiera zmontowane klipy audio."
        )

    if not audio_tracks:
        mixed = np.zeros(max_timeline_samples, dtype=np.float32)
        return mixed, _TARGET_SR, edit_rate, all_skipped_intervals, all_skipped_segments

    # Mix all audio tracks (average) into one signal.
    max_len = max(max(len(t) for t in audio_tracks), max_timeline_samples)
    mixed = np.zeros(max_len, dtype=np.float32)
    for track in audio_tracks:
        if len(track) < max_len:
            track = np.pad(track, (0, max_len - len(track)))
        mixed += track
    mixed /= len(audio_tracks)
    return mixed, _TARGET_SR, edit_rate, all_skipped_intervals, all_skipped_segments


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
) -> _TrackBuildResult:
    """
    Build a numpy audio array for one timeline slot.

    Iterates the sequence components:
    - SourceClip → resolve to MXF clip range, extract audio
    - Filler → silence of the appropriate duration

    Returns track audio (or None if no clip resolved), skipped ranges, and
    timeline length to keep global timing consistent even with failures.
    """
    chunks: list[np.ndarray] = []
    has_audio = False
    timeline_samples = 0
    skipped_intervals: list[tuple[float, float]] = []
    skipped_segments: list[SkippedSegment] = []

    for comp in components:
        cp = _props(comp)
        comp_type = type(comp).__name__
        length_frames = cp.get("Length", 0)

        if comp_type == "Filler":
            silence_samples = int(length_frames / edit_rate * _TARGET_SR)
            if silence_samples > 0:
                chunks.append(np.zeros(silence_samples, dtype=np.float32))
                timeline_samples += silence_samples

        elif comp_type == "SourceClip":
            src_id = str(cp.get("SourceID", ""))
            src_slot = cp.get("SourceMobSlotID")
            src_start = cp.get("StartTime", 0)
            if not src_id or src_slot is None:
                missed_sec = max(0.0, length_frames / edit_rate)
                missed_samples = int(round(missed_sec * _TARGET_SR))
                if missed_samples > 0:
                    start_sec = timeline_samples / _TARGET_SR
                    end_sec = (timeline_samples + missed_samples) / _TARGET_SR
                    skipped_intervals.append((start_sec, end_sec))
                    skipped_segments.append(
                        SkippedSegment(
                            file_name="<niezmapowany klip>",
                            reason="Błąd mapowania: SourceClip nie zawiera pełnych danych referencyjnych.",
                            duration_sec=end_sec - start_sec,
                        )
                    )
                    chunks.append(np.zeros(missed_samples, dtype=np.float32))
                    timeline_samples += missed_samples
                continue
            clip_refs = _resolve_clip(mob_by_id, src_id, src_slot, src_start, length_frames)
            if not clip_refs:
                missed_sec = max(0.0, length_frames / edit_rate)
                missed_samples = int(round(missed_sec * _TARGET_SR))
                if missed_samples > 0:
                    start_sec = timeline_samples / _TARGET_SR
                    end_sec = (timeline_samples + missed_samples) / _TARGET_SR
                    skipped_intervals.append((start_sec, end_sec))
                    skipped_segments.append(
                        SkippedSegment(
                            file_name="<niezmapowany klip>",
                            reason="Błąd mapowania: nie udało się powiązać SourceClip z plikiem MXF.",
                            duration_sec=end_sec - start_sec,
                        )
                    )
                    chunks.append(np.zeros(missed_samples, dtype=np.float32))
                    timeline_samples += missed_samples
                continue
            for ref in clip_refs:
                expected_samples = max(0, int(round(ref.duration_sec * _TARGET_SR)))
                if expected_samples == 0:
                    continue
                real_path = ref.mxf_path
                if not real_path.exists():
                    found = _find_mxf_on_any_drive(real_path.name)
                    if found is None:
                        start_sec = timeline_samples / _TARGET_SR
                        end_sec = (timeline_samples + expected_samples) / _TARGET_SR
                        skipped_intervals.append((start_sec, end_sec))
                        skipped_segments.append(
                            SkippedSegment(
                                file_name=real_path.name,
                                reason="Błąd mapowania: nie znaleziono pliku w lokalizacji AAF ani w Avid MediaFiles\\MXF.",
                                duration_sec=end_sec - start_sec,
                            )
                        )
                        chunks.append(np.zeros(expected_samples, dtype=np.float32))
                        timeline_samples += expected_samples
                        continue
                    real_path = found
                on_progress(f"Ekstrahuję klip: {real_path.name} [{ref.start_sec:.1f}s +{ref.duration_sec:.1f}s]…")
                try:
                    audio = _extract_clip_audio(real_path, ref.start_sec, ref.duration_sec)
                except AnalysisError:
                    start_sec = timeline_samples / _TARGET_SR
                    end_sec = (timeline_samples + expected_samples) / _TARGET_SR
                    skipped_intervals.append((start_sec, end_sec))
                    skipped_segments.append(
                        SkippedSegment(
                            file_name=real_path.name,
                            reason="Błąd odczytu: ffmpeg nie mógł wyekstrahować audio.",
                            duration_sec=end_sec - start_sec,
                        )
                    )
                    chunks.append(np.zeros(expected_samples, dtype=np.float32))
                    timeline_samples += expected_samples
                    continue
                audio = _fit_audio_length(audio, expected_samples)
                chunks.append(audio)
                timeline_samples += expected_samples
                if audio.size > 0:
                    has_audio = True

    return _TrackBuildResult(
        audio=np.concatenate(chunks) if has_audio and chunks else None,
        skipped_intervals=skipped_intervals,
        skipped_segments=skipped_segments,
        timeline_samples=timeline_samples,
    )


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


def _fit_audio_length(audio: np.ndarray, target_samples: int) -> np.ndarray:
    """Pad/trim extracted audio to expected timeline length."""
    if audio.size == target_samples:
        return audio
    if audio.size > target_samples:
        return audio[:target_samples]
    return np.pad(audio, (0, target_samples - audio.size))


# ---------------------------------------------------------------------------
# Pause detection
# ---------------------------------------------------------------------------

def _detect_pauses(
    y: np.ndarray,
    sr: int,
    threshold_sec: float,
    top_db: int = _TOP_DB,
) -> list[tuple[float, float]]:
    """
    Detect silent gaps in y longer than threshold_sec.

    Algorithm:
    1. librosa.effects.split() identifies non-silent intervals (RMS energy
       above top_db dB relative to signal peak).
    2. Non-silent intervals closer than _MERGE_GAP_SEC are merged — prevents
       soft speech transitions or brief energy dips from fragmenting speech
       into many small pieces and creating false short pauses between them.
    3. Gaps between merged non-silent intervals longer than threshold_sec
       are reported as pauses.

    Returns a list of (start_sec, end_sec) tuples.
    """
    if y.size == 0:
        return []

    raw_intervals = librosa.effects.split(y, top_db=top_db, frame_length=1024, hop_length=256)
    if len(raw_intervals) == 0:
        return []

    # Merge non-silent intervals that are less than _MERGE_GAP_SEC apart.
    merge_samples = int(_MERGE_GAP_SEC * sr)
    merged: list[list[int]] = [list(raw_intervals[0])]
    for start, end in raw_intervals[1:]:
        if start - merged[-1][1] <= merge_samples:
            merged[-1][1] = end   # extend current interval
        else:
            merged.append([start, end])

    total_samples = len(y)
    pauses: list[tuple[float, float]] = []

    prev_end = 0
    for start, end in merged:
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


def _merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Merge overlapping/adjacent intervals."""
    if not intervals:
        return []
    merged: list[list[float]] = []
    for start, end in sorted(intervals, key=lambda x: x[0]):
        if end <= start:
            continue
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def _subtract_intervals(
    base: list[tuple[float, float]],
    cut: list[tuple[float, float]],
    min_duration: float = 0.0,
) -> list[tuple[float, float]]:
    """Subtract cut intervals from base intervals."""
    if not base:
        return []
    cuts = _merge_intervals(cut)
    if not cuts:
        return [interval for interval in base if interval[1] - interval[0] >= min_duration]

    result: list[tuple[float, float]] = []
    for start, end in base:
        if end <= start:
            continue
        fragments = [(start, end)]
        for cut_start, cut_end in cuts:
            next_fragments: list[tuple[float, float]] = []
            for frag_start, frag_end in fragments:
                if cut_end <= frag_start or cut_start >= frag_end:
                    next_fragments.append((frag_start, frag_end))
                    continue
                if cut_start > frag_start:
                    next_fragments.append((frag_start, cut_start))
                if cut_end < frag_end:
                    next_fragments.append((cut_end, frag_end))
            fragments = next_fragments
            if not fragments:
                break
        for frag_start, frag_end in fragments:
            if frag_end - frag_start >= min_duration:
                result.append((frag_start, frag_end))
    return result


def _aggregate_skipped_segments(segments: list[SkippedSegment]) -> list[SkippedSegment]:
    """Aggregate skipped segments by file and reason."""
    grouped: dict[tuple[str, str], float] = {}
    for segment in segments:
        key = (segment.file_name, segment.reason)
        grouped[key] = grouped.get(key, 0.0) + segment.duration_sec
    return [
        SkippedSegment(file_name=file_name, reason=reason, duration_sec=duration_sec)
        for (file_name, reason), duration_sec in grouped.items()
    ]


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
