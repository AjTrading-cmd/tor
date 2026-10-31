#!/usr/bin/env python3
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ENGLISH_CODES = {"ENG", "EN", "ENGLISH"}


def get_media_info(file_path: Path):
    """Uses ffprobe to extract duration and audio stream metadata."""
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
        raise RuntimeError(f"ffprobe failed on {file_path.name}")

    data = json.loads(result.stdout)
    duration = float(data.get("format", {}).get("duration", 0.0))

    audio_streams = []
    audio_idx = 0
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "audio":
            codec = stream.get("codec_name", "unknown").upper()
            channels = stream.get("channels", 0)
            layout = stream.get("channel_layout", "")
            
            if "5.1" in layout or channels == 6:
                ch_str = "5.1"
            elif "7.1" in layout or channels == 8:
                ch_str = "7.1"
            elif channels == 2:
                ch_str = "2.0 Stereo"
            elif channels == 1:
                ch_str = "1.0 Mono"
            else:
                ch_str = f"{channels}ch"

            tags = stream.get("tags", {})
            lang = "UND"
            for k, v in tags.items():
                if k.lower() == "language":
                    lang = v.strip().upper()
                    break

            audio_streams.append({
                "stream_index": stream.get("index"),
                "audio_rel_index": audio_idx,  # 0-based index among audio tracks (0:a:X)
                "codec": codec,
                "channels": channels,
                "format_str": f"{codec} ({ch_str})",
                "lang": lang
            })
            audio_idx += 1

    return duration, audio_streams


def draw_progress_bar(filename: str, progress: float, bar_width: int = 30):
    """Renders an in-place terminal progress bar."""
    progress = min(max(progress, 0.0), 1.0)
    filled = int(bar_width * progress)
    bar = "█" * filled + "░" * (bar_width - filled)
    percent = progress * 100
    display_name = (filename[:32] + "...") if len(filename) > 35 else filename.ljust(35)
    sys.stdout.write(f"\r  {display_name} [{bar}] {percent:6.2f}%")
    sys.stdout.flush()


def convert_english_to_aac_dplii(
    input_path: Path,
    temp_path: Path,
    duration: float,
    keep_audio_indices: list[int],
    lfe_level: float
):
    """
    Copies video, subtitles, and attachments, maps ONLY the kept English audio
    tracks, discards all other audio tracks, and converts kept audio to AAC DPLII Stereo.
    """
    aresample_filter = (
        f"aresample=ocl=stereo:matrix_encoding=dplii:"
        f"lfe_mix_level={lfe_level}:clev=0.707:slev=0.707"
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(input_path),
        "-map", "0:v",       # Keep all video streams
    ]

    # Map ONLY the target English audio streams
    for a_idx in keep_audio_indices:
        cmd.extend(["-map", f"0:a:{a_idx}"])

    cmd.extend([
        "-map", "0:s?",      # Keep all subtitle streams (if any exist)
        "-map", "0:t?",      # Keep font attachments for subtitles (if any exist)
        "-c:v", "copy",
        "-c:s", "copy",
        "-c:t", "copy",
        "-af", aresample_filter,
        "-c:a", "aac",
        "-ac", "2",
        "-b:a", "256k",
        "-progress", "pipe:1",
        "-nostats",
        "-loglevel", "error",
        str(temp_path)
    ])

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1
    )

    draw_progress_bar(input_path.name, 0.0)

    try:
        for line in process.stdout:
            line = line.strip()
            if line.startswith("out_time_us="):
                val = line.split("=")[1]
                if val.isdigit() and duration > 0:
                    out_time_sec = int(val) / 1_000_000.0
                    draw_progress_bar(input_path.name, out_time_sec / duration)
            elif line == "progress=end":
                draw_progress_bar(input_path.name, 1.0)

        process.wait()
        print()  # Newline after progress bar finishes

        if process.returncode != 0:
            err_msg = process.stderr.read().strip()
            raise RuntimeError(f"FFmpeg error: {err_msg}")

    except KeyboardInterrupt:
        process.terminate()
        process.wait()
        if temp_path.exists():
            temp_path.unlink()
        print("\n\n[!] Interrupted by user. Cleaned up temporary file.")
        sys.exit(1)


def print_summary_table(results):
    """Pretty-prints the before/after audio conversion table."""
    headers = ["File Name", "Track", "Lang", "Before Format", "After Format", "Action / Status"]
    
    col_widths = [len(h) for h in headers]
    for row in results:
        for i, val in enumerate(row):
            col_widths[i] = max(col_widths[i], len(str(val)))

    def format_row(row_vals):
        return " | ".join(str(val).ljust(col_widths[i]) for i, val in enumerate(row_vals))

    separator = "-+-".join("-" * w for w in col_widths)

    print("\n" + "=" * len(separator))
    print(" AUDIO CONVERSION SUMMARY (English Only + Dolby Pro Logic II + LFE Mix)")
    print("=" * len(separator))
    print(format_row(headers))
    print(separator)
    for row in results:
        print(format_row(row))
    print("=" * len(separator) + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="Convert ONLY English audio tracks in .mkv files to AAC Stereo (DPLII) and discard non-English audio."
    )
    parser.add_argument(
        "folder",
        type=str,
        help="Path to the folder containing .mkv files"
    )
    parser.add_argument(
        "--keep-originals",
        action="store_true",
        help="Save converted files as *_stereo.mkv instead of replacing originals"
    )
    parser.add_argument(
        "--lfe",
        type=float,
        default=0.707,
        help="LFE (subwoofer bass) mix level (default: 0.707)"
    )
    args = parser.parse_args()

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("Error: 'ffmpeg' and 'ffprobe' must be installed. Run: sudo apt install ffmpeg")
        sys.exit(1)

    target_dir = Path(args.folder).expanduser().resolve()
    if not target_dir.is_dir():
        print(f"Error: Directory not found: {target_dir}")
        sys.exit(1)

    mkv_files = sorted([
        f for f in target_dir.glob("*.mkv")
        if not f.name.endswith(".temp_stereo.mkv")
    ])

    if not mkv_files:
        print(f"No .mkv files found in {target_dir}")
        sys.exit(0)

    print(f"Found {len(mkv_files)} .mkv file(s) in '{target_dir}'")
    print(f"Profile: English-Only | matrix_encoding=dplii | lfe_mix_level={args.lfe}\n")

    summary_rows = []

    for idx, mkv_file in enumerate(mkv_files, start=1):
        print(f"[{idx}/{len(mkv_files)}] Processing: {mkv_file.name}")

        try:
            duration, before_streams = get_media_info(mkv_file)
        except Exception as e:
            print(f"  [!] Skipping {mkv_file.name}: {e}")
            summary_rows.append([mkv_file.name, "-", "-", "ERROR", "ERROR", "Failed (Probe)"])
            continue

        if not before_streams:
            print(f"  [!] No audio tracks found in {mkv_file.name}, skipping.")
            summary_rows.append([mkv_file.name, "None", "-", "No Audio", "No Audio", "Skipped"])
            continue

        # 1. Identify English tracks
        eng_streams = [s for s in before_streams if s["lang"] in ENGLISH_CODES]
        used_fallback = False

        # 2. Fallback: If no tracks are explicitly tagged 'ENG', check for 'UND' (Undefined)
        if not eng_streams:
            und_streams = [s for s in before_streams if s["lang"] == "UND"]
            if und_streams:
                print("  [*] No explicit 'ENG' tag found; falling back to 'UND' (undefined) audio track(s).")
                eng_streams = und_streams
                used_fallback = True
            else:
                print(f"  [!] No English or Undefined audio tracks found in {mkv_file.name}! Skipping to avoid stripping all audio.")
                for t_idx, s in enumerate(before_streams):
                    summary_rows.append([
                        mkv_file.name if t_idx == 0 else "",
                        f"#{t_idx + 1}",
                        s["lang"],
                        s["format_str"],
                        s["format_str"],
                        "Skipped (No ENG Audio Found)"
                    ])
                continue

        keep_indices = [s["audio_rel_index"] for s in eng_streams]
        non_eng_exists = len(eng_streams) < len(before_streams)

        # 3. Check if the file ALREADY has only English AAC 2.0 Stereo (and no foreign tracks to strip)
        already_clean = (
            not non_eng_exists and
            all(s["codec"] == "AAC" and s["channels"] == 2 for s in eng_streams)
        )
        if already_clean:
            print("  [*] Already English-only AAC Stereo. Skipping.")
            for t_idx, s in enumerate(before_streams):
                summary_rows.append([
                    mkv_file.name if t_idx == 0 else "",
                    f"#{t_idx + 1}",
                    s["lang"],
                    s["format_str"],
                    s["format_str"],
                    "Skipped (Already Clean)"
                ])
            continue

        temp_output = mkv_file.with_name(f"{mkv_file.stem}.temp_stereo.mkv")

        try:
            convert_english_to_aac_dplii(
                mkv_file,
                temp_output,
                duration,
                keep_indices,
                args.lfe
            )

            if args.keep_originals:
                final_path = mkv_file.with_name(f"{mkv_file.stem}_stereo.mkv")
                temp_output.replace(final_path)
            else:
                temp_output.replace(mkv_file)
                final_path = mkv_file

            _, after_streams = get_media_info(final_path)
            after_fmt_str = (
                f"{after_streams[0]['format_str']} [DPLII]"
                if after_streams
                else "AAC (2.0 Stereo) [DPLII]"
            )

            # Log every original track and what happened to it
            for t_idx, b_stream in enumerate(before_streams):
                fname_col = mkv_file.name if t_idx == 0 else ""
                if b_stream["audio_rel_index"] in keep_indices:
                    status = "Converted (Fallback UND)" if used_fallback else "Converted & Kept"
                    summary_rows.append([
                        fname_col,
                        f"#{t_idx + 1}",
                        b_stream["lang"],
                        b_stream["format_str"],
                        after_fmt_str,
                        status
                    ])
                else:
                    summary_rows.append([
                        fname_col,
                        f"#{t_idx + 1}",
                        b_stream["lang"],
                        b_stream["format_str"],
                        "DISCARDED",
                        "Removed (Non-English)"
                    ])

        except Exception as e:
            print(f"  [!] Error converting {mkv_file.name}: {e}")
            if temp_output.exists():
                temp_output.unlink()
            for t_idx, b_stream in enumerate(before_streams):
                summary_rows.append([
                    mkv_file.name if t_idx == 0 else "",
                    f"#{t_idx + 1}",
                    b_stream["lang"],
                    b_stream["format_str"],
                    "N/A",
                    "Failed"
                ])

    print_summary_table(summary_rows)


if __name__ == "__main__":
    main()