#!/usr/bin/env python3
"""
watch_and_scan_media.py

Watches an incoming directory (or scans once via --once / cron) for .mkv and .mp4 files,
waits for file transfers to finish, scans each file for malware using ClamAV
(clamdscan or clamscan) plus media container hygiene checks, moves clean files to
`safe_files/` and flagged/suspicious files to `skeptical_files/`, and logs media
metadata (file type, resolution, audio format, subtitle format, safety notes) into
a monthly Markdown file in `docs/` (e.g., `docs/2026-11.md`).
"""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

VIDEO_EXTENSIONS = {".mkv", ".mp4"}
IGNORE_SUFFIXES = (".part", ".tmp", ".crdownload", ".partial", ".temp_stereo.mkv")


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
        # Summarize unique subtitle formats & languages cleanly
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
        # clamdscan uses the running daemon and is very fast; --fdpass passes file descriptor
        cmd_list = ["clamdscan", "--fdpass", "--no-summary", str(file_path)]
        scanner_name = "ClamAV (clamdscan)"
    elif shutil.which("clamscan"):
        # Increase max-filesize / max-scansize so larger video files can be scanned
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

    # ClamAV return codes:
    # 0 = No virus found
    # 1 = Virus(es) found
    # 2 = Some error(s) occurred
    if result.returncode == 0:
        return True, f"Clean — Passed {scanner_name}"
    elif result.returncode == 1:
        # Extract the signature name after '<filepath>: '
        finding = output.split(":", 1)[-1].strip() if ":" in output else output
        return False, f"INFECTED ({scanner_name}): {finding or 'Threat detected'}"
    else:
        # Fallback if clamdscan daemon is not running when clamdscan binary exists
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
    Creates or appends to the monthly Markdown log file (e.g., `docs/2026-11.md`).
    A new `.md` file is automatically started on the 1st of every month because
    the filename is keyed by `YYYY-MM`.
    """
    docs_dir.mkdir(parents=True, exist_ok=True)
    month_key = now.strftime("%Y-%m")
    month_title = now.strftime("%B %Y")
    md_path = docs_dir / f"{month_key}.md"

    if not md_path.exists() or md_path.stat().st_size == 0:
        header = (
            f"# Media Scan & Metadata Log — {month_title}\n\n"
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

    return md_path


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
) -> bool:
    """
    Full pipeline for a single detected video file:
    1. Wait until file write/transfer is complete.
    2. Inspect media streams & container integrity with ffprobe.
    3. Run ClamAV malware scan.
    4. Move to `safe_files` or `skeptical_files`.
    5. Log stats & safety notes to `docs/YYYY-MM.md`.
    """
    if not is_eligible_video_file(file_path):
        return False

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Detected: {file_path.name}")
    if stability_wait > 0:
        if not wait_until_file_stable(file_path, stability_seconds=stability_wait):
            print(f"  -> Skipped {file_path.name} (file disappeared or unstable)")
            return False

    # 1. Inspect media metadata
    media_info = inspect_media_file(file_path)

    # 2. Run malware scan
    scan_passed, scan_note = run_malware_scan(file_path, custom_scan_cmd=custom_scan_cmd)

    # 3. Combine ClamAV result with container hygiene checks
    notes = [scan_note]
    is_safe = scan_passed

    if not media_info.get("valid_media", True):
        is_safe = False
        notes.append("Invalid/unreadable video stream")

    if media_info.get("suspicious_attachments"):
        is_safe = False
        notes.extend(media_info["suspicious_attachments"])

    safety_notes_str = "; ".join(notes)

    # 4. Move to safe_files or skeptical_files
    if is_safe:
        dest_dir = safe_dir
        status_label = "SAFE"
    else:
        dest_dir = skeptical_dir
        status_label = "SKEPTICAL"

    final_path = move_to_destination(file_path, dest_dir)
    now = datetime.now()

    # 5. Update monthly Markdown report in docs/
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
    return True


def scan_existing_files(
    watch_dir: Path,
    safe_dir: Path,
    skeptical_dir: Path,
    docs_dir: Path,
    custom_scan_cmd: str | None = None,
    stability_wait: float = 2.0,
) -> int:
    """Scans all existing .mkv and .mp4 files currently in `watch_dir`."""
    count = 0
    for item in sorted(watch_dir.iterdir()):
        # Avoid scanning safe_dir, skeptical_dir, or docs_dir if they are subdirectories of watch_dir
        if item.resolve() in (safe_dir.resolve(), skeptical_dir.resolve(), docs_dir.resolve()):
            continue
        if is_eligible_video_file(item):
            if process_file(
                item,
                safe_dir=safe_dir,
                skeptical_dir=skeptical_dir,
                docs_dir=docs_dir,
                custom_scan_cmd=custom_scan_cmd,
                stability_wait=stability_wait,
            ):
                count += 1
    return count


def watch_directory(
    watch_dir: Path,
    safe_dir: Path,
    skeptical_dir: Path,
    docs_dir: Path,
    custom_scan_cmd: str | None = None,
    poll_interval: float = 3.0,
    stability_wait: float = 2.0,
):
    """
    Watches `watch_dir` in real time.
    Uses `inotifywait` if available for instant kernel-level event detection,
    while also doing periodic sweeps so no files are ever missed.
    """
    print(f"Watching directory:  {watch_dir}")
    print(f"Safe files dir:      {safe_dir}")
    print(f"Skeptical files dir: {skeptical_dir}")
    print(f"Monthly docs dir:    {docs_dir}")
    print("Press Ctrl+C to stop.\n")

    # Initial sweep for any files already sitting in the directory
    scan_existing_files(
        watch_dir,
        safe_dir=safe_dir,
        skeptical_dir=skeptical_dir,
        docs_dir=docs_dir,
        custom_scan_cmd=custom_scan_cmd,
        stability_wait=stability_wait,
    )

    has_inotifywait = shutil.which("inotifywait") is not None
    if has_inotifywait:
        print("Using inotifywait for real-time file event detection...")
        cmd = [
            "inotifywait",
            "-m",
            "-q",
            "-e", "close_write,moved_to",
            "--format", "%w%f",
            str(watch_dir),
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                candidate = Path(line.strip())
                if is_eligible_video_file(candidate):
                    process_file(
                        candidate,
                        safe_dir=safe_dir,
                        skeptical_dir=skeptical_dir,
                        docs_dir=docs_dir,
                        custom_scan_cmd=custom_scan_cmd,
                        stability_wait=stability_wait,
                    )
        except KeyboardInterrupt:
            print("\nStopping watcher...")
        finally:
            proc.terminate()
    else:
        print(f"Polling directory every {poll_interval}s (install 'inotify-tools' for inotify events)...")
        try:
            while True:
                scan_existing_files(
                    watch_dir,
                    safe_dir=safe_dir,
                    skeptical_dir=skeptical_dir,
                    docs_dir=docs_dir,
                    custom_scan_cmd=custom_scan_cmd,
                    stability_wait=stability_wait,
                )
                time.sleep(poll_interval)
        except KeyboardInterrupt:
            print("\nStopping watcher...")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Watch a directory for .mkv and .mp4 files, scan them with ClamAV, "
            "move them to safe_files or skeptical_files, and record monthly metadata in docs/YYYY-MM.md."
        )
    )
    parser.add_argument(
        "watch_dir",
        type=str,
        help="Path to the incoming directory to watch/scan for .mkv and .mp4 files",
    )
    parser.add_argument(
        "--safe-dir",
        type=str,
        default=None,
        help="Directory for files that pass the scan (default: <watch_dir>/../safe_files)",
    )
    parser.add_argument(
        "--skeptical-dir",
        type=str,
        default=None,
        help="Directory for files that fail the scan (default: <watch_dir>/../skeptical_files)",
    )
    parser.add_argument(
        "--docs-dir",
        type=str,
        default=str(Path(__file__).resolve().parent / "docs"),
        help="Directory for monthly Markdown logs (default: ./docs)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Scan existing files in watch_dir once and exit (useful for cronjobs)",
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
        default=2.0,
        help="Seconds to wait to verify file size stopped changing before scanning (default: 2.0)",
    )

    args = parser.parse_args()

    watch_dir = Path(args.watch_dir).expanduser().resolve()
    watch_dir.mkdir(parents=True, exist_ok=True)

    safe_dir = (
        Path(args.safe_dir).expanduser().resolve()
        if args.safe_dir
        else (watch_dir.parent / "safe_files").resolve()
    )
    skeptical_dir = (
        Path(args.skeptical_dir).expanduser().resolve()
        if args.skeptical_dir
        else (watch_dir.parent / "skeptical_files").resolve()
    )
    docs_dir = Path(args.docs_dir).expanduser().resolve()

    safe_dir.mkdir(parents=True, exist_ok=True)
    skeptical_dir.mkdir(parents=True, exist_ok=True)
    docs_dir.mkdir(parents=True, exist_ok=True)

    if args.once:
        processed = scan_existing_files(
            watch_dir,
            safe_dir=safe_dir,
            skeptical_dir=skeptical_dir,
            docs_dir=docs_dir,
            custom_scan_cmd=args.scanner_cmd,
            stability_wait=args.stability_wait,
        )
        print(f"Processed {processed} file(s).")
        sys.exit(0)

    watch_directory(
        watch_dir,
        safe_dir=safe_dir,
        skeptical_dir=skeptical_dir,
        docs_dir=docs_dir,
        custom_scan_cmd=args.scanner_cmd,
        stability_wait=args.stability_wait,
    )


if __name__ == "__main__":
    main()
