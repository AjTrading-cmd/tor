#!/usr/bin/env python3
"""
watch_and_scan_media.py

Can be triggered:
1. Directly by qBittorrent's "Run external program on torrent finished":
     /home/juju/ghe/tor/watch_and_scan_media.py "%F" --once
   where "%F" is either a single downloaded .mkv/.mp4 file or a torrent subfolder
   containing .mkv/.mp4 files.
2. As a one-shot directory scan (--once) or cronjob.
3. As a continuous real-time directory watcher (inotifywait / polling).

Workflow for each .mkv / .mp4 file:
- Scans for malware with ClamAV (clamdscan or clamscan) + ffprobe container checks.
- Moves clean files to `safe_files/` and flagged files to `skeptical_files/`.
- Updates the monthly Markdown log in `docs/YYYY-MM.md` (creating a new .md file
  each month starting on the 1st) and refreshes `docs/index.md` for GitHub / GitHub Pages.
- Automatically commits and pushes `docs/` updates to GitHub (can be disabled with `--no-git-push`).
"""

import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
import time

from split_season_video import detect_full_season_info, split_season_file

REPO_ROOT = Path(__file__).resolve().parent
SCAN_LOCK_FILE = Path("/tmp/tor_malware_scan_queue.lock")
SCAN_STATUS_FILE = Path("/tmp/tor_malware_scan_status.json")
VIDEO_EXTENSIONS = {".mkv", ".mp4"}
IGNORE_SUFFIXES = (
    ".!qb",
    ".part",
    ".tmp",
    ".crdownload",
    ".partial",
    ".temp_stereo.mkv",
)


def format_size(num_bytes: int) -> str:
    """Formats bytes into a human-readable string (e.g., 1.45 GB)."""
    size = float(max(num_bytes, 0))
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024.0 or unit == "TB":
            return f"{size:.2f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return f"{size:.2f} TB"


def format_duration(seconds: float) -> str:
    """Formats seconds as H:MM:SS."""
    seconds = int(max(seconds, 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def format_bitrate(bps: int) -> str:
    """Formats bits per second into kbps or Mbps."""
    if bps <= 0:
        return ""
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.1f} Mbps"
    return f"{bps // 1000} kbps"


def parse_fps(fps_str: str) -> str:
    """Parses ffprobe r_frame_rate / avg_frame_rate into a clean string."""
    if not fps_str or fps_str == "0/0":
        return ""
    if "/" in fps_str:
        num_s, den_s = fps_str.split("/", 1)
        try:
            num, den = float(num_s), float(den_s)
            if den > 0:
                val = num / den
                return f"{val:.3f}".rstrip("0").rstrip(".") + " fps"
        except ValueError:
            pass
    return ""


def get_resolution_label(width: int, height: int) -> str:
    """Returns a friendly resolution string such as '1920x1080 (1080p)'."""
    if width <= 0 or height <= 0:
        return "Unknown"
    if width >= 3800 or height >= 2100:
        tag = "4K"
    elif width >= 2500 or height >= 1400:
        tag = "1440p"
    elif width >= 1900 or height >= 1000:
        tag = "1080p"
    elif width >= 1200 or height >= 700:
        tag = "720p"
    elif height >= 480:
        tag = "480p"
    else:
        tag = "SD"
    return f"{width}x{height} ({tag})"


def detect_hdr_and_bitdepth(stream: dict) -> str:
    """Detects bit depth and HDR/Dolby Vision from video stream metadata."""
    pix_fmt = stream.get("pix_fmt", "")
    bits = stream.get("bits_per_raw_sample")
    bit_str = ""
    if bits and str(bits).isdigit():
        bit_str = f"{bits}-bit"
    elif "10" in pix_fmt:
        bit_str = "10-bit"
    elif "12" in pix_fmt:
        bit_str = "12-bit"
    elif pix_fmt:
        bit_str = "8-bit"

    color_transfer = stream.get("color_transfer", "").lower()
    side_data = stream.get("side_data_list", [])
    has_dovi = any(
        "dolby vision" in str(sd.get("side_data_type", "")).lower()
        for sd in side_data
    )

    hdr_str = ""
    if has_dovi:
        hdr_str = "DoVi"
    elif color_transfer in ("smpte2084", "arib-std-b67"):
        hdr_str = "HDR"

    parts = [p for p in (bit_str, hdr_str) if p]
    return " ".join(parts)


def get_channel_str(channels: int, layout: str) -> str:
    """Returns a clean channel layout string."""
    if "7.1" in layout or channels == 8:
        return "7.1"
    if "5.1" in layout or channels == 6:
        return "5.1"
    if channels == 2:
        return "2.0 Stereo"
    if channels == 1:
        return "1.0 Mono"
    if channels > 0:
        return f"{channels}ch"
    return "Unknown"


def inspect_media_file(file_path: Path) -> dict:
    """
    Runs ffprobe on a video file and extracts:
    - file_type (extension + container format + video codec)
    - resolution
    - audio_format
    - subtitle_format
    - suspicious_attachments (if any non-font/image attachments exist in the container)
    """
    ext = file_path.suffix.upper().lstrip(".") or "UNKNOWN"
    size_bytes = file_path.stat().st_size if file_path.exists() else 0

    if not shutil.which("ffprobe"):
        return {
            "file_type": ext,
            "size": format_size(size_bytes),
            "duration": "-",
            "resolution": "Unknown (ffprobe missing)",
            "audio_format": "Unknown (ffprobe missing)",
            "subtitle_format": "Unknown (ffprobe missing)",
            "suspicious_attachments": [],
            "valid_media": True,
        }

    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(file_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return {
            "file_type": f"{ext} (Unreadable/Corrupt)",
            "size": format_size(size_bytes),
            "duration": "-",
            "resolution": "Invalid container",
            "audio_format": "-",
            "subtitle_format": "-",
            "suspicious_attachments": ["ffprobe failed to parse container"],
            "valid_media": False,
        }

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {
            "file_type": f"{ext} (Parse Error)",
            "size": format_size(size_bytes),
            "duration": "-",
            "resolution": "Unknown",
            "audio_format": "-",
            "subtitle_format": "-",
            "suspicious_attachments": ["Invalid JSON from ffprobe"],
            "valid_media": False,
        }

    fmt = data.get("format", {})
    streams = data.get("streams", [])

    duration = float(fmt.get("duration", 0.0) or 0.0)
    size_bytes = int(fmt.get("size", 0) or size_bytes)

    video_streams = []
    audio_streams = []
    subtitle_streams = []
    suspicious_attachments = []

    allowed_attachment_exts = {
        ".ttf", ".otf", ".ttc", ".woff", ".woff2",
        ".jpg", ".jpeg", ".png", ".webp", ".xml", ".nfo"
    }

    for s in streams:
        ctype = s.get("codec_type")
        tags = s.get("tags", {}) or {}
        lang = "UND"
        for k, v in tags.items():
            if k.lower() == "language":
                lang = str(v).strip().upper()
                break

        if ctype == "video" and s.get("disposition", {}).get("attached_pic", 0) == 0:
            codec = s.get("codec_name", "unknown").upper()
            width = int(s.get("width", 0) or 0)
            height = int(s.get("height", 0) or 0)
            fps = parse_fps(s.get("avg_frame_rate") or s.get("r_frame_rate", ""))
            extra = detect_hdr_and_bitdepth(s)

            v_parts = [codec]
            if extra:
                v_parts.append(extra)
            if fps:
                v_parts.append(fps)

            video_streams.append({
                "resolution": get_resolution_label(width, height),
                "video_info": " ".join(v_parts),
            })

        elif ctype == "audio":
            codec = s.get("codec_name", "unknown").upper()
            profile = s.get("profile", "")
            if profile and profile.lower() not in ("unknown", "lc", codec.lower()):
                codec_display = f"{codec} ({profile})"
            else:
                codec_display = codec

            channels = int(s.get("channels", 0) or 0)
            layout = s.get("channel_layout", "")
            ch_str = get_channel_str(channels, layout)

            a_bps = int(s.get("bit_rate", 0) or 0)
            br_str = f" @ {format_bitrate(a_bps)}" if a_bps > 0 else ""

            audio_streams.append(f"[{lang}] {codec_display} {ch_str}{br_str}")

        elif ctype == "subtitle":
            sub_codec = s.get("codec_name", "unknown").upper()
            subtitle_streams.append(f"{sub_codec} ({lang})")

        elif ctype == "attachment":
            filename = ""
            mimetype = ""
            for k, v in tags.items():
                if k.lower() == "filename":
                    filename = str(v).strip()
                elif k.lower() == "mimetype":
                    mimetype = str(v).strip().lower()
            att_ext = Path(filename).suffix.lower() if filename else ""
            if att_ext and att_ext not in allowed_attachment_exts:
                suspicious_attachments.append(
                    f"Suspicious attachment: {filename} ({mimetype or 'unknown mime'})"
                )

    if video_streams:
        res_str = video_streams[0]["resolution"]
        file_type_str = f"{ext} ({video_streams[0]['video_info']})"
    else:
        res_str = "No Video"
        file_type_str = f"{ext} (No Video Stream)"

    audio_str = "<br>".join(audio_streams) if audio_streams else "None"

    if subtitle_streams:
        unique_subs = list(dict.fromkeys(subtitle_streams))
        if len(unique_subs) > 4:
            sub_str = ", ".join(unique_subs[:4]) + f" (+{len(unique_subs) - 4} more)"
        else:
            sub_str = ", ".join(unique_subs)
    else:
        sub_str = "None"

    return {
        "file_type": file_type_str,
        "size": format_size(size_bytes),
        "duration": format_duration(duration),
        "resolution": res_str,
        "audio_format": audio_str,
        "subtitle_format": sub_str,
        "suspicious_attachments": suspicious_attachments,
        "valid_media": bool(video_streams),
    }


def run_malware_scan(file_path: Path, custom_scan_cmd: str | None = None) -> tuple[bool, str]:
    """
    Runs ClamAV (clamdscan or clamscan) or a custom malware scanner on `file_path`.
    Returns (is_safe: bool, safety_notes: str).
    """
    if custom_scan_cmd:
        cmd_list = custom_scan_cmd.split() + [str(file_path)]
        scanner_name = cmd_list[0]
    elif shutil.which("clamdscan"):
        cmd_list = ["clamdscan", "--fdpass", "--no-summary", str(file_path)]
        scanner_name = "ClamAV (clamdscan)"
    elif shutil.which("clamscan"):
        cmd_list = [
            "clamscan",
            "--no-summary",
            "--max-filesize=2000M",
            "--max-scansize=2000M",
            str(file_path),
        ]
        scanner_name = "ClamAV (clamscan)"
    else:
        return (
            False,
            "Skeptical: ClamAV (`clamscan` / `clamdscan`) is not installed on system",
        )

    try:
        result = subprocess.run(cmd_list, capture_output=True, text=True)
    except Exception as exc:
        return False, f"Skeptical: Scanner error ({exc})"

    output = (result.stdout or "").strip()
    err_output = (result.stderr or "").strip()

    if result.returncode == 0:
        return True, f"Clean — Passed {scanner_name}"
    elif result.returncode == 1:
        finding = output.split(":", 1)[-1].strip() if ":" in output else output
        return False, f"INFECTED ({scanner_name}): {finding or 'Threat detected'}"
    else:
        if scanner_name == "ClamAV (clamdscan)" and shutil.which("clamscan"):
            fallback_cmd = [
                "clamscan",
                "--no-summary",
                "--max-filesize=2000M",
                "--max-scansize=2000M",
                str(file_path),
            ]
            fb_res = subprocess.run(fallback_cmd, capture_output=True, text=True)
            fb_out = (fb_res.stdout or "").strip()
            if fb_res.returncode == 0:
                return True, "Clean — Passed ClamAV (clamscan)"
            elif fb_res.returncode == 1:
                finding = fb_out.split(":", 1)[-1].strip() if ":" in fb_out else fb_out
                return False, f"INFECTED (ClamAV clamscan): {finding}"

        detail = err_output or output or f"Exit code {result.returncode}"
        return False, f"Skeptical — Scan error ({scanner_name}): {detail}"


def wait_until_file_stable(file_path: Path, stability_seconds: float = 2.0, timeout: float = 600.0) -> bool:
    """
    Waits until a file's size and mtime stop changing so partially copied/downloaded
    files aren't scanned prematurely.
    """
    start = time.time()
    last_size = -1
    last_mtime = -1.0

    while time.time() - start < timeout:
        if not file_path.exists():
            return False
        try:
            stat = file_path.stat()
        except OSError:
            return False

        if stat.st_size > 0 and stat.st_size == last_size and stat.st_mtime == last_mtime:
            return True

        last_size = stat.st_size
        last_mtime = stat.st_mtime
        time.sleep(stability_seconds)

    return file_path.exists()


def move_to_destination(src: Path, dest_dir: Path) -> Path:
    """
    Moves `src` into `dest_dir`, avoiding filename collisions by appending a timestamp
    if a file with the same name already exists.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / src.name
    if target.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = dest_dir / f"{src.stem}_{stamp}{src.suffix}"
    shutil.move(str(src), str(target))
    return target


def escape_md_cell(text: str) -> str:
    """Escapes pipe characters and newlines for Markdown table cells."""
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def update_docs_index(docs_dir: Path) -> Path:
    """
    Regenerates `docs/index.md` so GitHub and GitHub Pages have a clean landing page
    linking to every monthly report (`YYYY-MM.md`) along with scan counts.
    """
    docs_dir.mkdir(parents=True, exist_ok=True)
    month_pattern = re.compile(r"^\d{4}-\d{2}\.md$")
    monthly_files = sorted(
        [f for f in docs_dir.iterdir() if f.is_file() and month_pattern.match(f.name)],
        reverse=True,
    )

    lines = [
        "# Media Malware Scan & Metadata Logs",
        "",
        "Automated monthly logs generated by [`watch_and_scan_media.py`](../watch_and_scan_media.py) when torrents finish in **qBittorrent**.",
        "",
        "Each `.mkv` and `.mp4` file is scanned with **ClamAV** (`clamdscan` / `clamscan`) and inspected with **`ffprobe`** before being sorted into `safe_files/` or `skeptical_files/`.",
        "",
        "## Monthly Reports",
        "",
    ]

    if not monthly_files:
        lines.append("*No media files have been scanned yet. Monthly `.md` logs will appear here automatically.*")
    else:
        lines.append("| Month | Report Link | Total Scanned | Safe (`safe_files`) | Skeptical (`skeptical_files`) |")
        lines.append("| :--- | :--- | :---: | :---: | :---: |")
        for mf in monthly_files:
            month_stem = mf.stem
            try:
                month_label = datetime.strptime(month_stem, "%Y-%m").strftime("%B %Y")
            except ValueError:
                month_label = month_stem

            content_lines = mf.read_text(encoding="utf-8", errors="replace").splitlines()
            data_rows = [
                ln for ln in content_lines
                if ln.startswith("| ") and not ln.startswith("| Date / Time") and not ln.startswith("| :---")
            ]
            total_count = len(data_rows)
            safe_count = sum(1 for r in data_rows if "| SAFE |" in r)
            skeptical_count = sum(1 for r in data_rows if "| SKEPTICAL |" in r)

            lines.append(
                f"| **{month_label}** | [`{mf.name}`](./{mf.name}) | {total_count} | {safe_count} | {skeptical_count} |"
            )

    lines.append("")
    index_path = docs_dir / "index.md"
    index_path.write_text("\n".join(lines), encoding="utf-8")
    return index_path


def log_to_monthly_markdown(
    docs_dir: Path,
    now: datetime,
    file_name: str,
    status_label: str,
    dest_folder_name: str,
    media_info: dict,
    safety_notes: str,
) -> Path:
    """
    Creates or appends to the monthly Markdown log file (e.g., `docs/2026-11.md`)
    and refreshes `docs/index.md` for GitHub / GitHub Pages.
    A new `.md` file is automatically started on the 1st of every month.
    """
    docs_dir.mkdir(parents=True, exist_ok=True)
    month_key = now.strftime("%Y-%m")
    month_title = now.strftime("%B %Y")
    md_path = docs_dir / f"{month_key}.md"

    if not md_path.exists() or md_path.stat().st_size == 0:
        header = (
            f"# Media Scan & Metadata Log — {month_title}\n\n"
            f"[← Back to All Monthly Reports](./index.md)\n\n"
            f"Monthly log of `.mp4` and `.mkv` files scanned starting **{month_key}-01**.\n\n"
            "| Date / Time | File Name | Status | Destination | File Type | Size | Duration | Resolution | Audio Format | Subtitle Format | Safety Notes |\n"
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n"
        )
        md_path.write_text(header, encoding="utf-8")

    row_cells = [
        now.strftime("%Y-%m-%d %H:%M:%S"),
        f"`{escape_md_cell(file_name)}`",
        escape_md_cell(status_label),
        f"`{escape_md_cell(dest_folder_name)}/`",
        escape_md_cell(media_info["file_type"]),
        escape_md_cell(media_info["size"]),
        escape_md_cell(media_info["duration"]),
        escape_md_cell(media_info["resolution"]),
        escape_md_cell(media_info["audio_format"]),
        escape_md_cell(media_info["subtitle_format"]),
        escape_md_cell(safety_notes),
    ]
    row_line = "| " + " | ".join(row_cells) + " |\n"

    with md_path.open("a", encoding="utf-8") as f:
        f.write(row_line)

    update_docs_index(docs_dir)
    return md_path


def git_commit_and_push_docs(docs_dir: Path, processed_names: list[str]):
    """
    Stages `docs/` in the git repository, commits the updated monthly Markdown log,
    and pushes to the remote GitHub repository.
    """
    if not shutil.which("git"):
        print("  -> Git not found; skipping auto-commit.")
        return

    try:
        # Stage docs directory
        subprocess.run(
            ["git", "-C", str(REPO_ROOT), "add", str(docs_dir)],
            check=True,
            capture_output=True,
            text=True,
        )
        # Check if there are staged changes in docs/
        diff_check = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "diff", "--cached", "--quiet", "--", str(docs_dir)]
        )
        if diff_check.returncode == 0:
            return

        summary = ", ".join(processed_names[:3])
        if len(processed_names) > 3:
            summary += f" (+{len(processed_names) - 3} more)"
        commit_msg = f"docs: log media scan for {summary}"

        subprocess.run(
            ["git", "-C", str(REPO_ROOT), "commit", "-m", commit_msg],
            check=True,
            capture_output=True,
            text=True,
        )
        push_res = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "push"],
            capture_output=True,
            text=True,
        )
        if push_res.returncode == 0:
            print("  -> Committed and pushed updated /docs to GitHub.")
        else:
            print(f"  -> Committed locally, but 'git push' failed: {push_res.stderr.strip()}")
    except Exception as exc:
        print(f"  -> Warning: Failed to git commit/push docs: {exc}")


def update_scan_status(**kwargs):
    """Writes live telemetry to `/tmp/tor_malware_scan_status.json` for the status UI."""
    try:
        current = {}
        if SCAN_STATUS_FILE.exists():
            try:
                current = json.loads(SCAN_STATUS_FILE.read_text(encoding="utf-8"))
            except Exception:
                current = {}
        current.update(kwargs)
        current["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        current["pid"] = os.getpid()
        tmp_path = SCAN_STATUS_FILE.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(current, indent=2), encoding="utf-8")
        tmp_path.replace(SCAN_STATUS_FILE)
    except Exception:
        pass


@contextmanager
def acquire_malware_scan_lock():
    """
    Ensures only ONE malware scan / season-split / docs-commit operation runs at a time,
    even if qBittorrent spawns multiple `watch_and_scan_media.py` processes simultaneously.
    Subsequent invocations wait in an OS-level FIFO lock queue until the active scan finishes.
    """
    lock_fd = open(SCAN_LOCK_FILE, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(
                f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                f"[Queue] Another malware scan is currently running (PID {os.getpid()} waiting in queue)..."
            )
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
            print(
                f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                f"[Queue] Lock acquired by PID {os.getpid()}; starting scan."
            )
        yield
    finally:
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
        finally:
            lock_fd.close()


def is_eligible_video_file(file_path: Path) -> bool:
    """Checks if a path is a regular .mkv or .mp4 file and not a temporary/partial file."""
    if not file_path.is_file():
        return False
    name_lower = file_path.name.lower()
    if name_lower.startswith("."):
        return False
    if any(name_lower.endswith(sfx) for sfx in IGNORE_SUFFIXES):
        return False
    return file_path.suffix.lower() in VIDEO_EXTENSIONS


def process_file(
    file_path: Path,
    safe_dir: Path,
    skeptical_dir: Path,
    docs_dir: Path,
    custom_scan_cmd: str | None = None,
    stability_wait: float = 2.0,
) -> str | None:
    """
    Full pipeline for a single detected video file.
    Returns the final filename if processed, or None if skipped.
    """
    if not is_eligible_video_file(file_path):
        return None

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Processing: {file_path}")
    update_scan_status(
        state="WAITING_STABLE",
        current_file=file_path.name,
        file_size=format_size(file_path.stat().st_size) if file_path.exists() else "-",
        stage="Waiting for file write to stabilize",
    )
    if stability_wait > 0:
        if not wait_until_file_stable(file_path, stability_seconds=stability_wait):
            print(f"  -> Skipped {file_path.name} (file disappeared or unstable)")
            update_scan_status(state="IDLE", current_file="", stage="Idle")
            return None

    update_scan_status(
        state="INSPECTING",
        current_file=file_path.name,
        file_size=format_size(file_path.stat().st_size),
        stage="Inspecting streams & container with ffprobe",
    )
    media_info = inspect_media_file(file_path)

    update_scan_status(
        state="SCANNING_MALWARE",
        current_file=file_path.name,
        file_size=media_info["size"],
        stage="Running ClamAV malware scan",
    )
    scan_passed, scan_note = run_malware_scan(file_path, custom_scan_cmd=custom_scan_cmd)

    notes = [scan_note]
    is_safe = scan_passed

    if not media_info.get("valid_media", True):
        is_safe = False
        notes.append("Invalid/unreadable video stream")

    if media_info.get("suspicious_attachments"):
        is_safe = False
        notes.extend(media_info["suspicious_attachments"])

    safety_notes_str = "; ".join(notes)

    if is_safe:
        # Check if this clean file is a combined full-season video file
        season_info = detect_full_season_info(file_path)
        if season_info is not None:
            update_scan_status(
                state="SPLITTING_SEASON",
                current_file=file_path.name,
                stage=f"Splitting season into {season_info['episode_count']} episodes",
            )
            try:
                created_episodes = split_season_file(
                    file_path=file_path,
                    output_dir=safe_dir,
                    remove_original=True,
                )
            except Exception as exc:
                created_episodes = []
                print(f"  [!] Season split failed ({exc}); moving unsplit file to safe_files/")

            if created_episodes:
                now = datetime.now()
                split_note = (
                    f"{safety_notes_str}; Split from full-season file `{file_path.name}` "
                    f"({len(created_episodes)} episodes)"
                )
                for ep_path in created_episodes:
                    ep_info = inspect_media_file(ep_path)
                    md_path = log_to_monthly_markdown(
                        docs_dir=docs_dir,
                        now=now,
                        file_name=ep_path.name,
                        status_label="SAFE",
                        dest_folder_name=safe_dir.name,
                        media_info=ep_info,
                        safety_notes=split_note,
                    )
                print(
                    f"  -> Status: SAFE (Split into {len(created_episodes)} episodes in {safe_dir})"
                )
                update_scan_status(
                    state="IDLE",
                    current_file="",
                    stage="Idle",
                    last_file=f"{file_path.name} ({len(created_episodes)} eps)",
                    last_result="SAFE (Season Split)",
                    last_finished_at=now.strftime("%Y-%m-%d %H:%M:%S"),
                )
                return f"{file_path.name} ({len(created_episodes)} episodes)"

        dest_dir = safe_dir
        status_label = "SAFE"
    else:
        dest_dir = skeptical_dir
        status_label = "SKEPTICAL"

    final_path = move_to_destination(file_path, dest_dir)
    now = datetime.now()

    md_path = log_to_monthly_markdown(
        docs_dir=docs_dir,
        now=now,
        file_name=final_path.name,
        status_label=status_label,
        dest_folder_name=dest_dir.name,
        media_info=media_info,
        safety_notes=safety_notes_str,
    )

    print(
        f"  -> Status: {status_label} | Moved to: {final_path} | Logged to: {md_path}"
    )
    update_scan_status(
        state="IDLE",
        current_file="",
        stage="Idle",
        last_file=final_path.name,
        last_result=status_label,
        last_finished_at=now.strftime("%Y-%m-%d %H:%M:%S"),
    )
    return final_path.name


def scan_target_path(
    target_path: Path,
    safe_dir: Path,
    skeptical_dir: Path,
    docs_dir: Path,
    custom_scan_cmd: str | None = None,
    stability_wait: float = 2.0,
) -> list[str]:
    """
    Scans `target_path`, which can be:
    - A single .mkv or .mp4 file (e.g., passed by qBittorrent `%F` for a single-file torrent)
    - A directory or multi-file torrent folder (recursively finds .mkv and .mp4 files inside it)
    """
    processed_names: list[str] = []
    excluded_dirs = {safe_dir.resolve(), skeptical_dir.resolve(), docs_dir.resolve()}

    if target_path.is_file():
        name = process_file(
            target_path,
            safe_dir=safe_dir,
            skeptical_dir=skeptical_dir,
            docs_dir=docs_dir,
            custom_scan_cmd=custom_scan_cmd,
            stability_wait=stability_wait,
        )
        if name:
            processed_names.append(name)
        return processed_names

    if not target_path.is_dir():
        print(f"Warning: Target path does not exist: {target_path}")
        return processed_names

    for item in sorted(target_path.rglob("*")):
        # Skip anything inside safe_files, skeptical_files, or docs
        if any(ex in item.resolve().parents or item.resolve() == ex for ex in excluded_dirs):
            continue
        if is_eligible_video_file(item):
            name = process_file(
                item,
                safe_dir=safe_dir,
                skeptical_dir=skeptical_dir,
                docs_dir=docs_dir,
                custom_scan_cmd=custom_scan_cmd,
                stability_wait=stability_wait,
            )
            if name:
                processed_names.append(name)

    return processed_names


def watch_directory(
    watch_dir: Path,
    safe_dir: Path,
    skeptical_dir: Path,
    docs_dir: Path,
    custom_scan_cmd: str | None = None,
    poll_interval: float = 3.0,
    stability_wait: float = 2.0,
    git_push: bool = True,
):
    """
    Watches `watch_dir` in real time using `inotifywait` (if available) or polling.
    Uses an internal FIFO worker queue AND the cross-process file lock so files are
    always scanned strictly one at a time without blocking event detection.
    """
    print(f"Watching directory:  {watch_dir}")
    print(f"Safe files dir:      {safe_dir}")
    print(f"Skeptical files dir: {skeptical_dir}")
    print(f"Monthly docs dir:    {docs_dir}")
    print("Press Ctrl+C to stop.\n")

    file_queue: queue.Queue[Path] = queue.Queue()
    queued_or_active: set[Path] = set()
    state_lock = threading.Lock()
    stop_event = threading.Event()
    excluded_dirs = {safe_dir.resolve(), skeptical_dir.resolve(), docs_dir.resolve()}

    def enqueue_candidate(candidate: Path):
        resolved = candidate.resolve()
        if any(ex in resolved.parents or resolved == ex for ex in excluded_dirs):
            return
        if not is_eligible_video_file(resolved):
            return
        with state_lock:
            if resolved in queued_or_active:
                return
            queued_or_active.add(resolved)
            q_size = file_queue.qsize() + 1
        if q_size > 1:
            print(f"  [Queue] Enqueued {resolved.name} (position #{q_size} in malware scan queue)")
        file_queue.put(resolved)
        update_scan_status(queued_count=file_queue.qsize(), watch_dir=str(watch_dir))

    def worker_loop():
        while not stop_event.is_set():
            try:
                candidate = file_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            update_scan_status(queued_count=file_queue.qsize())
            try:
                if is_eligible_video_file(candidate):
                    with acquire_malware_scan_lock():
                        name = process_file(
                            candidate,
                            safe_dir=safe_dir,
                            skeptical_dir=skeptical_dir,
                            docs_dir=docs_dir,
                            custom_scan_cmd=custom_scan_cmd,
                            stability_wait=stability_wait,
                        )
                        if name and git_push:
                            git_commit_and_push_docs(docs_dir, [name])
            finally:
                with state_lock:
                    queued_or_active.discard(candidate)
                file_queue.task_done()
                update_scan_status(queued_count=file_queue.qsize())

    worker = threading.Thread(target=worker_loop, daemon=True)
    worker.start()

    def sweep_directory():
        for item in sorted(watch_dir.rglob("*")):
            enqueue_candidate(item)

    sweep_directory()

    has_inotifywait = shutil.which("inotifywait") is not None
    if has_inotifywait:
        print("Using inotifywait + FIFO queue for real-time file event detection...")
        cmd = [
            "inotifywait",
            "-m",
            "-r",
            "-q",
            "-e", "close_write,moved_to",
            "--format", "%w%f",
            str(watch_dir),
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                enqueue_candidate(Path(line.strip()))
        except KeyboardInterrupt:
            print("\nStopping watcher...")
        finally:
            stop_event.set()
            proc.terminate()
    else:
        print(f"Polling directory every {poll_interval}s (install 'inotify-tools' for inotify events)...")
        try:
            while True:
                sweep_directory()
                time.sleep(poll_interval)
        except KeyboardInterrupt:
            print("\nStopping watcher...")
        finally:
            stop_event.set()


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Scan incoming .mkv and .mp4 files (from qBittorrent 'Run external program', "
            "cronjob, or real-time folder watch) with ClamAV, move to safe_files or "
            "skeptical_files, and log monthly stats in docs/YYYY-MM.md."
        )
    )
    parser.add_argument(
        "target",
        type=str,
        help=(
            "Path to a finished torrent file/folder (e.g. qBittorrent \"%%F\") "
            "or an incoming directory to watch/scan"
        ),
    )
    parser.add_argument(
        "--safe-dir",
        type=str,
        default=None,
        help="Directory for files that pass the scan (default: <target_parent>/safe_files)",
    )
    parser.add_argument(
        "--skeptical-dir",
        type=str,
        default=None,
        help="Directory for files that fail the scan (default: <target_parent>/skeptical_files)",
    )
    parser.add_argument(
        "--docs-dir",
        type=str,
        default=str(REPO_ROOT / "docs"),
        help="Directory for monthly Markdown logs (default: /home/juju/ghe/tor/docs)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Scan the target file/directory once and exit (automatically enabled if target is a file)",
    )
    parser.add_argument(
        "--no-git-push",
        action="store_true",
        help="Do not automatically git commit & push updated /docs files to GitHub",
    )
    parser.add_argument(
        "--scanner-cmd",
        type=str,
        default=None,
        help="Optional custom malware scanner command (defaults to clamdscan / clamscan)",
    )
    parser.add_argument(
        "--stability-wait",
        type=float,
        default=1.0,
        help="Seconds to verify file size stopped changing before scanning (default: 1.0)",
    )

    args = parser.parse_args()

    target_path = Path(args.target).expanduser().resolve()
    docs_dir = Path(args.docs_dir).expanduser().resolve()

    # Determine base parent folder when computing default safe_files / skeptical_files paths
    base_parent = target_path.parent if target_path.is_file() else target_path.parent

    safe_dir = (
        Path(args.safe_dir).expanduser().resolve()
        if args.safe_dir
        else (base_parent / "safe_files").resolve()
    )
    skeptical_dir = (
        Path(args.skeptical_dir).expanduser().resolve()
        if args.skeptical_dir
        else (base_parent / "skeptical_files").resolve()
    )

    safe_dir.mkdir(parents=True, exist_ok=True)
    skeptical_dir.mkdir(parents=True, exist_ok=True)
    docs_dir.mkdir(parents=True, exist_ok=True)
    update_docs_index(docs_dir)

    # Single-file targets (like qBittorrent "%F" on a single-file torrent) always run once
    run_once = args.once or target_path.is_file()

    if run_once:
        with acquire_malware_scan_lock():
            processed = scan_target_path(
                target_path,
                safe_dir=safe_dir,
                skeptical_dir=skeptical_dir,
                docs_dir=docs_dir,
                custom_scan_cmd=args.scanner_cmd,
                stability_wait=args.stability_wait,
            )
            if processed and not args.no_git_push:
                git_commit_and_push_docs(docs_dir, processed)
            print(f"Processed {len(processed)} file(s).")
        sys.exit(0)

    target_path.mkdir(parents=True, exist_ok=True)
    watch_directory(
        target_path,
        safe_dir=safe_dir,
        skeptical_dir=skeptical_dir,
        docs_dir=docs_dir,
        custom_scan_cmd=args.scanner_cmd,
        stability_wait=args.stability_wait,
        git_push=not args.no_git_push,
    )


if __name__ == "__main__":
    main()
