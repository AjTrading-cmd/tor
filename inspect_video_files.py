#!/usr/bin/env python3
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

VIDEO_EXTENSIONS = {".mkv", ".mp4"}


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
        return "-"
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.1f} Mbps"
    return f"{bps // 1000} kbps"


def parse_fps(fps_str: str) -> str:
    """Parses ffprobe r_frame_rate / avg_frame_rate (e.g. '24000/1001') into a clean string."""
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
    has_dovi = any("dolby vision" in str(sd.get("side_data_type", "")).lower() for sd in side_data)

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


def inspect_video_file(file_path: Path) -> dict:
    """Runs ffprobe on a video file and extracts video, audio, subtitle, and container stats."""
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(file_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError("ffprobe failed")

    data = json.loads(result.stdout)
    fmt = data.get("format", {})
    streams = data.get("streams", [])

    duration = float(fmt.get("duration", 0.0) or 0.0)
    size_bytes = int(fmt.get("size", 0) or file_path.stat().st_size)
    overall_bitrate = int(fmt.get("bit_rate", 0) or 0)

    video_streams = []
    audio_streams = []
    subtitle_langs = []

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
                "video_info": " / ".join(v_parts)
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

            title = ""
            for k, v in tags.items():
                if k.lower() == "title":
                    title = str(v).strip()
                    break
            dplii_tag = " [DPLII]" if "dplii" in title.lower() else ""

            audio_streams.append(
                f"[{lang}] {codec_display} {ch_str}{br_str}{dplii_tag}"
            )

        elif ctype == "subtitle":
            subtitle_langs.append(lang)

    res_str = video_streams[0]["resolution"] if video_streams else "No Video"
    vid_str = video_streams[0]["video_info"] if video_streams else "-"

    if not audio_streams:
        audio_list = ["No Audio"]
    else:
        audio_list = [f"#{i + 1} {a}" for i, a in enumerate(audio_streams)]

    if subtitle_langs:
        unique_langs = list(dict.fromkeys(subtitle_langs))
        shown = ", ".join(unique_langs[:3])
        if len(unique_langs) > 3:
            shown += ", ..."
        subs_str = f"{len(subtitle_langs)} ({shown})"
    else:
        subs_str = "None"

    return {
        "size": format_size(size_bytes),
        "size_bytes": size_bytes,
        "duration": format_duration(duration),
        "duration_sec": duration,
        "bitrate": format_bitrate(overall_bitrate),
        "resolution": res_str,
        "video": vid_str,
        "audio_tracks": audio_list,
        "subtitles": subs_str,
    }


def print_table(rows: list[list[str]], total_files: int, total_bytes: int, total_duration: float, target_dir: Path):
    """Pretty-prints the media inspection table."""
    headers = [
        "File Name",
        "Size",
        "Duration",
        "Resolution",
        "Video (Codec / Bit / FPS)",
        "Bitrate",
        "Audio Track(s)",
        "Subtitles"
    ]

    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            col_widths[i] = max(col_widths[i], len(str(val)))

    def format_row(row_vals):
        return " | ".join(str(val).ljust(col_widths[i]) for i, val in enumerate(row_vals))

    separator = "-+-".join("-" * w for w in col_widths)

    print("\n" + "=" * len(separator))
    print(f" VIDEO MEDIA SUMMARY ({target_dir})")
    print("=" * len(separator))
    print(format_row(headers))
    print(separator)
    for row in rows:
        print(format_row(row))
    print(separator)
    print(
        f" Total Files: {total_files} | "
        f"Total Size: {format_size(total_bytes)} | "
        f"Total Duration: {format_duration(total_duration)}"
    )
    print("=" * len(separator) + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="Inspect all .mkv and .mp4 files in a directory (including subdirectories) and pretty-print video/audio stats."
    )
    parser.add_argument(
        "folder",
        type=str,
        help="Path to the directory (or single .mkv/.mp4 file) to inspect"
    )
    parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="Only inspect the top-level directory (do not search subdirectories)"
    )
    args = parser.parse_args()

    if not shutil.which("ffprobe"):
        print("Error: 'ffprobe' must be installed. Run: sudo apt install ffmpeg")
        sys.exit(1)

    target_path = Path(args.folder).expanduser().resolve()
    if target_path.is_file() and target_path.suffix.lower() in VIDEO_EXTENSIONS:
        target_dir = target_path.parent
        video_files = [target_path]
    elif target_path.is_dir():
        target_dir = target_path
        glob_func = target_dir.glob if args.no_recursive else target_dir.rglob
        video_files = sorted([
            f for f in glob_func("*")
            if f.is_file()
            and f.suffix.lower() in VIDEO_EXTENSIONS
            and not f.name.endswith(".temp_stereo.mkv")
        ])
    else:
        print(f"Error: Directory or .mkv/.mp4 file not found: {target_path}")
        sys.exit(1)

    if not video_files:
        print(f"No .mkv or .mp4 files found in '{target_dir}'")
        sys.exit(0)

    rows = []
    total_bytes = 0
    total_duration = 0.0

    for video_file in video_files:
        rel_name = str(video_file.relative_to(target_dir))
        try:
            info = inspect_video_file(video_file)
            total_bytes += info["size_bytes"]
            total_duration += info["duration_sec"]

            for idx, audio_str in enumerate(info["audio_tracks"]):
                if idx == 0:
                    rows.append([
                        rel_name,
                        info["size"],
                        info["duration"],
                        info["resolution"],
                        info["video"],
                        info["bitrate"],
                        audio_str,
                        info["subtitles"],
                    ])
                else:
                    rows.append(["", "", "", "", "", "", audio_str, ""])
        except Exception as e:
            rows.append([rel_name, "ERROR", "-", "-", "-", "-", str(e), "-"])

    print_table(rows, len(video_files), total_bytes, total_duration, target_dir)


if __name__ == "__main__":
    main()
