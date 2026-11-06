#!/usr/bin/env python3
"""
split_season_video.py

Detects whether an .mkv or .mp4 file is a combined full-season (or multi-episode)
video file and splits it losslessly (via `ffmpeg -c copy` without re-encoding) into
individual episode files (e.g. `Show.Name.S01E01.mkv`, `Show.Name.S01E02.mkv`, ...).

How episode boundaries are determined (in priority order):
1. Embedded Chapters (`ffprobe -show_chapters`):
   - If the number of episode-length chapters matches the expected episode count,
     uses those exact chapter timestamps.
   - Or if chapters are grouped (e.g. 4-6 chapters per episode), groups chapters
     near each expected episode boundary.
2. Black-frame / Silence boundary refinement (`ffmpeg blackdetect` / `silencedetect`):
   - Searches a window around each expected episode transition for black transitions
     or audio silence to find the exact cut point between episodes.
3. Equal-duration fallback:
   - Splits evenly across the expected episode count if no chapters or black frames
     are found in the transition window.

Can be used standalone from the CLI or imported by `watch_and_scan_media.py`.
"""

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

VIDEO_EXTENSIONS = {".mkv", ".mp4"}

# Patterns that indicate a single video file contains an entire season or multi-episode range:
# 1. Explicit episode range: S01E01-E10, S01E01-10, S01E01_E08, S1E1-E6, etc.
EPISODE_RANGE_RE = re.compile(
    r"(?<![a-zA-Z0-9])[sS](\d{1,2})[\.\-_\s]?[eE](\d{1,3})\s*(?:[\-~_]|to)\s*[eE]?(\d{1,3})(?![a-zA-Z0-9])",
    re.IGNORECASE,
)

# 2. Season-only marker without any single-episode marker:
#    e.g. "Show.Name.S01.1080p.mkv", "Show Name - Season 1 Complete.mkv", "Show.S02.COMPLETE.mp4"
SEASON_ONLY_RE = re.compile(
    r"(?<![a-zA-Z0-9])(?:[sS]eason[\.\-_\s]?(\d{1,2})|[sS](\d{2}))(?![a-zA-Z0-9])",
    re.IGNORECASE,
)
SINGLE_EPISODE_RE = re.compile(
    r"(?<![a-zA-Z0-9])(?:[sS]\d{1,2}[\.\-_\s]?[eE]\d{1,3}|\d{1,2}[xX]\d{2,3}|(?:[eE]pisode|[eE]p)[\.\-_\s]?\d{1,3})(?![a-zA-Z0-9])"
)
EXPLICIT_EP_COUNT_RE = re.compile(
    r"(?<![a-zA-Z0-9])(\d{1,2})[\.\-_\s]?(?:[eE]pisodes|[eE]ps)(?![a-zA-Z0-9])"
)


def format_duration(seconds: float) -> str:
    """Formats seconds as H:MM:SS."""
    seconds = int(max(seconds, 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def probe_duration_and_chapters(file_path: Path) -> tuple[float, list[dict]]:
    """
    Returns `(duration_seconds, chapters_list)` using `ffprobe`.
    Each chapter dict has `start`, `end`, `duration`, and `title`.
    """
    if not shutil.which("ffprobe"):
        return 0.0, []

    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_chapters",
        str(file_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        return 0.0, []

    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError:
        return 0.0, []

    duration = float(data.get("format", {}).get("duration", 0.0) or 0.0)
    raw_chapters = data.get("chapters", []) or []

    chapters = []
    for ch in raw_chapters:
        start = float(ch.get("start_time", 0.0) or 0.0)
        end = float(ch.get("end_time", 0.0) or 0.0)
        title = str((ch.get("tags") or {}).get("title", "")).strip()
        if end > start:
            chapters.append({
                "start": start,
                "end": end,
                "duration": end - start,
                "title": title,
            })

    return duration, chapters


def detect_full_season_info(file_path: Path) -> dict | None:
    """
    Determines whether `file_path` is a combined full-season (or multi-episode) video file.
    Returns a dict with:
      - `season`: int
      - `start_ep`: int
      - `end_ep`: int
      - `episode_count`: int
      - `duration`: float
      - `chapters`: list[dict]
      - `reason`: str
    or `None` if it is a normal single episode or movie.
    """
    if not file_path.is_file() or file_path.suffix.lower() not in VIDEO_EXTENSIONS:
        return None

    name = file_path.stem
    duration, chapters = probe_duration_and_chapters(file_path)

    # Case 1: Explicit episode range in filename (e.g., S01E01-E10 or S01E01-04)
    range_match = EPISODE_RANGE_RE.search(name)
    if range_match:
        season_num = int(range_match.group(1))
        start_ep = int(range_match.group(2))
        end_ep = int(range_match.group(3))
        if end_ep > start_ep and (end_ep - start_ep + 1) <= 50:
            ep_count = end_ep - start_ep + 1
            return {
                "season": season_num,
                "start_ep": start_ep,
                "end_ep": end_ep,
                "episode_count": ep_count,
                "duration": duration,
                "chapters": chapters,
                "reason": f"Filename episode range S{season_num:02d}E{start_ep:02d}-E{end_ep:02d}",
            }

    # Case 2: Filename has a Season marker (e.g. "S01" or "Season 1") but NO single-episode marker ("S01E01")
    season_match = SEASON_ONLY_RE.search(name)
    has_single_ep = SINGLE_EPISODE_RE.search(name) is not None

    if season_match and not has_single_ep:
        season_num = int(season_match.group(1) or season_match.group(2))

        # Check if filename explicitly mentions episode count (e.g. "10 Episodes" or "8Eps")
        ep_count_match = EXPLICIT_EP_COUNT_RE.search(name)
        if ep_count_match:
            ep_count = int(ep_count_match.group(1))
            if 2 <= ep_count <= 50:
                return {
                    "season": season_num,
                    "start_ep": 1,
                    "end_ep": ep_count,
                    "episode_count": ep_count,
                    "duration": duration,
                    "chapters": chapters,
                    "reason": f"Season S{season_num:02d} with explicit count ({ep_count} episodes)",
                }

        # Check if embedded chapters represent individual episodes (each 10 to 90 minutes)
        ep_like_chapters = [
            ch for ch in chapters if 600.0 <= ch["duration"] <= 5400.0
        ]
        if len(ep_like_chapters) >= 2 and len(ep_like_chapters) == len(chapters):
            ep_count = len(ep_like_chapters)
            return {
                "season": season_num,
                "start_ep": 1,
                "end_ep": ep_count,
                "episode_count": ep_count,
                "duration": duration,
                "chapters": chapters,
                "reason": f"Season S{season_num:02d} with {ep_count} episode-sized chapters",
            }

        # Or if total duration is long enough to be a full season (>= 2.5 hours / 9000s)
        if duration >= 9000.0:
            # Estimate episode count from chapters or standard 45-min / 22-min episode lengths
            if len(ep_like_chapters) >= 3:
                ep_count = len(ep_like_chapters)
            else:
                # Default heuristic: ~45 min (2700s) per episode for long-form TV
                ep_count = max(2, round(duration / 2700.0))
            return {
                "season": season_num,
                "start_ep": 1,
                "end_ep": ep_count,
                "episode_count": ep_count,
                "duration": duration,
                "chapters": chapters,
                "reason": f"Season S{season_num:02d} combined file ({format_duration(duration)})",
            }

    return None


def find_black_or_silence_near(
    file_path: Path, target_sec: float, window_sec: float = 90.0
) -> float | None:
    """
    Uses `ffmpeg` blackdetect and silencedetect in a small window `[target_sec - window_sec, target_sec + window_sec]`
    to find the cleanest transition point closest to `target_sec`.
    """
    if not shutil.which("ffmpeg"):
        return None

    start_win = max(0.0, target_sec - window_sec)
    duration_win = window_sec * 2.0

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-ss", f"{start_win:.2f}",
        "-t", f"{duration_win:.2f}",
        "-i", str(file_path),
        "-vf", "blackdetect=d=0.5:pix_th=0.10",
        "-af", "silencedetect=n=-45dB:d=0.7",
        "-f", "null",
        "-",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception:
        return None

    stderr = res.stderr or ""
    candidates: list[float] = []

    # Parse black_start / black_end
    for m in re.finditer(r"black_start:(\d+(?:\.\d+)?)\s+black_end:(\d+(?:\.\d+)?)", stderr):
        b_start = float(m.group(1))
        b_end = float(m.group(2))
        mid = start_win + (b_start + b_end) / 2.0
        candidates.append(mid)

    # If no black frames found, check silence_end
    if not candidates:
        for m in re.finditer(r"silence_end:\s*(\d+(?:\.\d+)?)", stderr):
            s_end = float(m.group(1))
            candidates.append(start_win + s_end)

    if not candidates:
        return None

    # Return candidate closest to target_sec
    return min(candidates, key=lambda t: abs(t - target_sec))


def compute_episode_segments(
    file_path: Path,
    duration: float,
    chapters: list[dict],
    episode_count: int,
    use_media_detection: bool = True,
) -> list[tuple[float, float]]:
    """
    Computes `(start_sec, end_sec)` for each episode using:
    1. Exact chapter match if `len(chapters) == episode_count` (or filtered episode chapters).
    2. Nearest chapter boundary if multi-chapter per episode.
    3. Black-frame / silence detection near expected boundaries.
    4. Equal-duration fallback.
    """
    if episode_count <= 1 or duration <= 0:
        return [(0.0, max(duration, 0.0))]

    # 1. Direct 1-to-1 chapter match
    if len(chapters) == episode_count:
        return [(ch["start"], ch["end"]) for ch in chapters]

    # Filter out tiny intro/outro padding chapters (< 120s) and check again
    substantial_chapters = [ch for ch in chapters if ch["duration"] >= 120.0]
    if len(substantial_chapters) == episode_count:
        segments = []
        for idx, ch in enumerate(substantial_chapters):
            start_t = 0.0 if idx == 0 else ch["start"]
            end_t = duration if idx == episode_count - 1 else substantial_chapters[idx + 1]["start"]
            segments.append((start_t, end_t))
        return segments

    avg_ep_len = duration / episode_count
    cut_points = [0.0]
    chapter_boundaries = [ch["start"] for ch in chapters if ch["start"] > 0]

    for i in range(1, episode_count):
        expected_t = i * avg_ep_len
        chosen_t = None

        # 2. Check if there is a chapter boundary within 15% of expected_t
        if chapter_boundaries:
            nearest_ch = min(chapter_boundaries, key=lambda t: abs(t - expected_t))
            if abs(nearest_ch - expected_t) <= avg_ep_len * 0.20:
                chosen_t = nearest_ch

        # 3. Otherwise refine with black-frame / silence detection
        if chosen_t is None and use_media_detection:
            detected_t = find_black_or_silence_near(
                file_path, expected_t, window_sec=min(90.0, avg_ep_len * 0.15)
            )
            if detected_t is not None:
                chosen_t = detected_t

        # 4. Fallback to equal division
        if chosen_t is None:
            chosen_t = expected_t

        cut_points.append(chosen_t)

    cut_points.append(duration)

    segments: list[tuple[float, float]] = []
    for i in range(episode_count):
        segments.append((cut_points[i], cut_points[i + 1]))
    return segments


def build_episode_filename(original_path: Path, season: int, episode: int) -> str:
    """
    Constructs a clean episode filename like `Show.Name.S01E01.1080p.mkv` from the
    combined season filename.
    """
    stem = original_path.stem
    ext = original_path.suffix
    ep_tag = f"S{season:02d}E{episode:02d}"

    # Replace explicit range if present (e.g. S01E01-E10 -> S01E01)
    if EPISODE_RANGE_RE.search(stem):
        new_stem = EPISODE_RANGE_RE.sub(ep_tag, stem, count=1)
        return f"{new_stem}{ext}"

    # Replace standalone season marker (e.g. S01 or Season 1 -> S01E01)
    if SEASON_ONLY_RE.search(stem):
        new_stem = SEASON_ONLY_RE.sub(ep_tag, stem, count=1)
        return f"{new_stem}{ext}"

    return f"{stem}.{ep_tag}{ext}"


def split_season_file(
    file_path: Path,
    output_dir: Path | None = None,
    episode_count_override: int | None = None,
    season_override: int | None = None,
    remove_original: bool = True,
    dry_run: bool = False,
) -> list[Path]:
    """
    Splits a combined full-season `.mkv` or `.mp4` file into individual episode files
    in `output_dir` (defaults to `file_path.parent`).
    If `remove_original` is True and all episode files are created successfully,
    deletes `file_path`.
    Returns the list of created episode `Path`s (or empty list if not a combined season file).
    """
    if not shutil.which("ffmpeg"):
        raise RuntimeError("'ffmpeg' is required to split season files. Run: sudo apt install ffmpeg")

    info = detect_full_season_info(file_path)
    if info is None and episode_count_override is None:
        return []

    duration, chapters = (
        (info["duration"], info["chapters"])
        if info
        else probe_duration_and_chapters(file_path)
    )
    season = season_override or (info["season"] if info else 1)
    start_ep = info["start_ep"] if info else 1
    ep_count = episode_count_override or (info["episode_count"] if info else 1)
    reason = info["reason"] if info else f"Manual override ({ep_count} episodes)"

    if ep_count <= 1 or duration <= 0:
        return []

    dest_dir = (output_dir or file_path.parent).expanduser().resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)

    print(f"  [Season Splitter] Detected combined season file: {file_path.name}")
    print(f"    Reason:   {reason}")
    print(f"    Duration: {format_duration(duration)} -> Splitting into {ep_count} episodes (S{season:02d}E{start_ep:02d}..E{start_ep + ep_count - 1:02d})")

    segments = compute_episode_segments(
        file_path=file_path,
        duration=duration,
        chapters=chapters,
        episode_count=ep_count,
        use_media_detection=not dry_run,
    )

    created_files: list[Path] = []

    for idx, (seg_start, seg_end) in enumerate(segments):
        ep_num = start_ep + idx
        ep_name = build_episode_filename(file_path, season, ep_num)
        out_path = dest_dir / ep_name
        seg_len = max(0.0, seg_end - seg_start)

        print(
            f"    -> [{idx + 1}/{ep_count}] {ep_name} "
            f"({format_duration(seg_start)} - {format_duration(seg_end)}, {format_duration(seg_len)})"
        )

        if dry_run:
            created_files.append(out_path)
            continue

        # Write to a hidden temporary file first so watchers (like rsync_mkv_to_remote.py --watch)
        # only see the episode file once ffmpeg finishes writing it completely
        temp_out = dest_dir / f".splitting_{ep_name}.part"
        if temp_out.exists():
            temp_out.unlink()

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-ss", f"{seg_start:.3f}",
            "-to", f"{seg_end:.3f}",
            "-i", str(file_path),
            "-map", "0",
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            str(temp_out),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0 or not temp_out.exists() or temp_out.stat().st_size == 0:
            # Clean up any partial output and abort removing original
            if temp_out.exists():
                temp_out.unlink()
            err = (res.stderr or "").strip()
            raise RuntimeError(f"ffmpeg failed while splitting {ep_name}: {err}")

        temp_out.replace(out_path)
        created_files.append(out_path)

    if not dry_run and remove_original and len(created_files) == ep_count:
        try:
            file_path.unlink()
            print(f"  [Season Splitter] Removed original combined file: {file_path.name}")
        except OSError as exc:
            print(f"  [Season Splitter] Warning: Could not remove original file {file_path.name}: {exc}")

    return created_files


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Detect and losslessly split combined full-season .mkv and .mp4 files into "
            "individual episode files using embedded chapters, black/silence detection, or duration."
        )
    )
    parser.add_argument(
        "target",
        type=str,
        help="Path to a combined season .mkv/.mp4 file or a directory containing video files",
    )
    parser.add_argument(
        "-o", "--output-dir",
        type=str,
        default=None,
        help="Output directory for the split episode files (default: same folder as source file)",
    )
    parser.add_argument(
        "-e", "--episodes",
        type=int,
        default=None,
        help="Explicitly specify the number of episodes to split into (overrides auto-detection)",
    )
    parser.add_argument(
        "-s", "--season",
        type=int,
        default=None,
        help="Explicitly specify the season number (e.g., 1 for S01)",
    )
    parser.add_argument(
        "--keep-original",
        action="store_true",
        help="Keep the original combined season file after splitting (default: delete original)",
    )
    parser.add_argument(
        "-n", "--dry-run",
        action="store_true",
        help="Preview how the file(s) would be split without writing files",
    )

    args = parser.parse_args()
    target_path = Path(args.target).expanduser().resolve()
    out_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else None

    if target_path.is_file():
        files = [target_path]
    elif target_path.is_dir():
        files = sorted([
            f for f in target_path.rglob("*")
            if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS and not f.name.startswith(".")
        ])
    else:
        print(f"Error: File or directory not found: {target_path}")
        sys.exit(1)

    total_split = 0
    for f in files:
        created = split_season_file(
            file_path=f,
            output_dir=out_dir,
            episode_count_override=args.episodes,
            season_override=args.season,
            remove_original=not args.keep_original,
            dry_run=args.dry_run,
        )
        if created:
            total_split += 1

    if total_split == 0:
        print("No combined full-season video files detected.")
    else:
        print(f"Split {total_split} combined season file(s).")


if __name__ == "__main__":
    main()
