#!/usr/bin/env python3
import argparse
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROGRESS_RE = re.compile(r"^\s*(\d+)\s+(\d+)%\s+(\S+/s)\s+(\S+)")
VIDEO_EXTENSIONS = {".mkv", ".mp4"}


def format_time(seconds: float) -> str:
    """Formats seconds as H:MM:SS."""
    seconds = int(max(seconds, 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def format_size(num_bytes: int) -> str:
    """Formats bytes into a human-readable string (e.g., 1.45 GB)."""
    size = float(max(num_bytes, 0))
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024.0 or unit == "TB":
            return f"{size:.2f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return f"{size:.2f} TB"


def draw_progress_bar(
    filename: str,
    progress: float,
    start_time: float,
    speed_str: str = "",
    bar_width: int = 30
):
    """Renders an in-place terminal progress bar with elapsed time, ETA, and speed."""
    progress = min(max(progress, 0.0), 1.0)
    filled = int(bar_width * progress)
    bar = "█" * filled + "░" * (bar_width - filled)
    percent = progress * 100
    elapsed = time.monotonic() - start_time
    if progress >= 1.0:
        eta_str = "0:00:00"
    elif progress > 0.0 and elapsed > 0:
        eta_str = format_time(elapsed * (1.0 - progress) / progress)
    else:
        eta_str = "--:--:--"
    display_name = (filename[:32] + "...") if len(filename) > 35 else filename.ljust(35)
    speed_suffix = f" ({speed_str})" if speed_str else ""
    sys.stdout.write(
        f"\r  {display_name} [{bar}] {percent:6.2f}% "
        f"Elapsed: {format_time(elapsed)} ETA: {eta_str}{speed_suffix}\033[K"
    )
    sys.stdout.flush()


def sync_single_file(
    source_dir: Path,
    video_file: Path,
    destination: str,
    ssh_cmd: list[str],
    dry_run: bool,
    remove_source_files: bool
) -> tuple[str, str, float, bool]:
    """
    Rsyncs a single video file (.mkv/.mp4) directly into destination (without creating parent subdirectories)
    while rendering a live progress bar with Elapsed & ETA.
    Returns (status_str, last_speed_str, elapsed_seconds, was_transferred).
    """
    display_name = video_file.name
    file_size = video_file.stat().st_size

    cmd = [
        "rsync",
        "-av",
        "--itemize-changes",
        "--no-human-readable",
        "--progress",
        "--partial",
        "--protect-args",
        "-e", shlex.join(ssh_cmd),
    ]

    if dry_run:
        cmd.append("--dry-run")

    if remove_source_files:
        cmd.append("--remove-source-files")

    # Copy only the video file itself directly into destination/ without its parent directory
    cmd.extend([str(video_file), f"{destination}/"])

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )

    start_time = time.monotonic()
    draw_progress_bar(display_name, 0.0, start_time)

    last_speed = ""
    itemized_transfer = False
    buf = bytearray()

    try:
        while True:
            chunk = process.stdout.read(1)
            if not chunk:
                break
            if chunk in (b"\r", b"\n"):
                if buf:
                    line = buf.decode("utf-8", errors="replace").strip()
                    buf.clear()

                    if line.startswith((">f", "<f", "cf", "hf")):
                        itemized_transfer = True
                    elif "(xfr#" in line:
                        itemized_transfer = True

                    match = PROGRESS_RE.match(line)
                    if match:
                        bytes_done = int(match.group(1))
                        pct = int(match.group(2)) / 100.0
                        last_speed = match.group(3)
                        progress = (bytes_done / file_size) if file_size > 0 else pct
                        draw_progress_bar(display_name, progress, start_time, last_speed)
            else:
                buf.extend(chunk)

        process.wait()
        elapsed = time.monotonic() - start_time
        draw_progress_bar(display_name, 1.0, start_time, last_speed)
        print()  # Newline after progress bar completes

        if process.returncode != 0:
            err_msg = process.stderr.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(err_msg or f"rsync exited with code {process.returncode}")

        if dry_run:
            status = "Dry Run (Would Sync)" if itemized_transfer else "Dry Run (Up to Date)"
        elif itemized_transfer:
            status = "Synced & Removed Local" if remove_source_files else "Synced"
        else:
            status = "Skipped (Up to Date)"

        if not itemized_transfer:
            last_speed = "-"
        elif not last_speed and elapsed > 0:
            last_speed = f"{format_size(int(file_size / elapsed))}/s"

        return status, last_speed or "-", elapsed, itemized_transfer

    except KeyboardInterrupt:
        process.terminate()
        process.wait()
        print("\n\n[!] Transfer interrupted by user.")
        sys.exit(1)


def print_summary_table(
    results: list[list[str]],
    total_files: int,
    synced_count: int,
    skipped_count: int,
    failed_count: int,
    total_bytes_synced: int,
    total_elapsed: float,
    destination: str
):
    """Pretty-prints the rsync transfer summary table and overall statistics."""
    headers = ["File Name", "Size", "Avg Speed", "Time Elapsed", "Action / Status"]

    col_widths = [len(h) for h in headers]
    for row in results:
        for i, val in enumerate(row):
            col_widths[i] = max(col_widths[i], len(str(val)))

    def format_row(row_vals):
        return " | ".join(str(val).ljust(col_widths[i]) for i, val in enumerate(row_vals))

    separator = "-+-".join("-" * w for w in col_widths)

    print("\n" + "=" * len(separator))
    print(f" RSYNC TRANSFER SUMMARY -> {destination}")
    print("=" * len(separator))
    print(format_row(headers))
    print(separator)
    for row in results:
        print(format_row(row))
    print(separator)
    print(
        f" Total Files: {total_files} (Synced: {synced_count}, "
        f"Up to Date: {skipped_count}, Failed: {failed_count}) | "
        f"Transferred: {format_size(total_bytes_synced)} | "
        f"Total Time: {format_time(total_elapsed)}"
    )
    print("=" * len(separator) + "\n")


def prompt_file_selection(video_entries: list[tuple[Path, str]]) -> list[tuple[Path, str]]:
    """
    Displays a numbered list of discovered video files and prompts the user to select
    which files to sync (supports single numbers, comma/space lists, ranges like 1-3, or 'all').
    """
    print("\nAvailable video files:")
    for idx, (vf, rel_name) in enumerate(video_entries, start=1):
        size_str = format_size(vf.stat().st_size)
        print(f"  [{idx}] {rel_name} ({size_str})")

    while True:
        try:
            raw = input(
                "\nSelect file(s) to sync (e.g., 1,3 or 1-3, 'all', or 'q' to cancel): "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            sys.exit(0)

        if not raw or raw.lower() in ("q", "quit", "exit"):
            print("Cancelled.")
            sys.exit(0)

        if raw.lower() in ("a", "all", "*"):
            return video_entries

        selected_indices: list[int] = []
        valid = True
        tokens = [t for part in raw.split(",") for t in part.split()]
        if not tokens:
            valid = False

        for token in tokens:
            if "-" in token:
                parts = token.split("-", 1)
                if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                    start_i, end_i = int(parts[0]), int(parts[1])
                    if 1 <= start_i <= end_i <= len(video_entries):
                        for i in range(start_i, end_i + 1):
                            if i not in selected_indices:
                                selected_indices.append(i)
                        continue
                print(f"  [!] Invalid range: '{token}' (choose between 1 and {len(video_entries)})")
                valid = False
                break
            elif token.isdigit():
                idx = int(token)
                if 1 <= idx <= len(video_entries):
                    if idx not in selected_indices:
                        selected_indices.append(idx)
                else:
                    print(f"  [!] Number out of range: {idx} (choose between 1 and {len(video_entries)})")
                    valid = False
                    break
            else:
                print(f"  [!] Invalid selection: '{token}'")
                valid = False
                break

        if valid and selected_indices:
            print()
            return [video_entries[i - 1] for i in selected_indices]


def parse_args():
    parser = argparse.ArgumentParser(
        description="Rsync .mkv and .mp4 video files from local file(s) or directory(ies) to a remote server."
    )
    parser.add_argument(
        "source_dir",
        type=str,
        help="Path to a local directory or .mkv/.mp4 file to sync"
    )
    parser.add_argument(
        "remote_server",
        type=str,
        help="Remote server address (e.g., user@192.168.1.100 or user@host:/path/to/movies), or additional local file(s)"
    )
    parser.add_argument(
        "remote_dir",
        type=str,
        nargs="*",
        default=[],
        help="Destination directory on the remote server (e.g., /path/to/remote/movies). "
             "Can also be provided directly in remote_server as user@host:/path/to/remote/movies"
    )
    parser.add_argument(
        "-s", "--select",
        action="store_true",
        help="Interactively select which .mkv/.mp4 files to sync from a numbered list"
    )
    parser.add_argument(
        "-p", "--port",
        type=int,
        default=22,
        help="SSH port on the remote server (default: 22)"
    )
    parser.add_argument(
        "-i", "--identity",
        type=str,
        default=None,
        help="Path to SSH private key file (optional)"
    )
    parser.add_argument(
        "-r", "--recursive",
        action="store_true",
        default=True,
        help="Recursively find and sync .mkv/.mp4 files in subdirectories (enabled by default)"
    )
    parser.add_argument(
        "--no-recursive",
        action="store_false",
        dest="recursive",
        help="Do not search subdirectories"
    )
    parser.add_argument(
        "-n", "--dry-run",
        action="store_true",
        help="Perform a trial run with no changes made"
    )
    parser.add_argument(
        "--remove-source-files",
        action="store_true",
        help="Remove local .mkv/.mp4 files after they are successfully transferred"
    )
    return parser.parse_args()


def resolve_positional_args(args) -> tuple[list[str], str, str]:
    """
    Separates one or more local source paths from the remote server and destination directory.
    Supports both:
      - <source(s)...> user@host:/remote/path
      - <source(s)...> user@host /remote/path
    """
    all_pos = [args.source_dir, args.remote_server] + list(args.remote_dir)

    last_arg = all_pos[-1]
    if ":" in last_arg and not Path(last_arg).expanduser().exists():
        local_sources = all_pos[:-1]
        remote_host, remote_path = last_arg.split(":", 1)
        if not remote_path:
            print("Error: Remote directory path cannot be empty.")
            sys.exit(1)
        return local_sources, remote_host, remote_path

    if len(all_pos) >= 3:
        local_sources = all_pos[:-2]
        remote_host = all_pos[-2].rstrip(":")
        remote_path = all_pos[-1]
        return local_sources, remote_host, remote_path

    print(
        "Error: Please specify the destination directory on the remote server.\n"
        "Example: ./rsync_mkv_to_remote.py /local/movies user@debian-host /remote/movies\n"
        "     or: ./rsync_mkv_to_remote.py /local/movie.mkv user@debian-host:/remote/movies"
    )
    sys.exit(1)


def main():
    args = parse_args()

    if not shutil.which("rsync") or not shutil.which("ssh"):
        print("Error: 'rsync' and 'ssh' must be installed. Run: sudo apt install rsync openssh-client")
        sys.exit(1)

    local_sources, remote_host, remote_path = resolve_positional_args(args)

    video_entries: list[tuple[Path, str]] = []
    seen_paths: set[Path] = set()

    for src in local_sources:
        src_path = Path(src).expanduser().resolve()
        if src_path.is_file():
            if src_path.suffix.lower() not in VIDEO_EXTENSIONS:
                print(f"Error: '{src_path}' is not a supported video file (.mkv or .mp4)")
                sys.exit(1)
            if src_path not in seen_paths:
                seen_paths.add(src_path)
                video_entries.append((src_path, src_path.name))
        elif src_path.is_dir():
            glob_func = src_path.rglob if args.recursive else src_path.glob
            found = sorted([
                f for f in glob_func("*")
                if f.is_file()
                and f.suffix.lower() in VIDEO_EXTENSIONS
                and not f.name.endswith(".temp_stereo.mkv")
            ])
            for f in found:
                if f not in seen_paths:
                    seen_paths.add(f)
                    video_entries.append((f, str(f.relative_to(src_path))))
        else:
            print(f"Error: Local file or directory not found: {src_path}")
            sys.exit(1)

    if not video_entries:
        print(f"No .mkv or .mp4 video files found in {', '.join(local_sources)}")
        sys.exit(0)

    # Reuse a single SSH connection across files so credentials are only requested once
    ssh_cmd = [
        "ssh",
        "-p", str(args.port),
        "-o", "ControlMaster=auto",
        "-o", "ControlPath=/tmp/rsync-mkv-%r@%h:%p",
        "-o", "ControlPersist=60s",
    ]
    if args.identity:
        identity_path = Path(args.identity).expanduser().resolve()
        if not identity_path.is_file():
            print(f"Error: SSH identity file not found: {identity_path}")
            sys.exit(1)
        ssh_cmd.extend(["-i", str(identity_path)])

    destination = f"{remote_host}:{remote_path}"
    total_size = sum(f.stat().st_size for f, _ in video_entries)

    print(f"Found {len(video_entries)} video file(s) (.mkv/.mp4, {format_size(total_size)})")
    print(f"Destination: {destination}")

    if args.select:
        video_entries = prompt_file_selection(video_entries)
        selected_size = sum(f.stat().st_size for f, _ in video_entries)
        print(f"Selected {len(video_entries)} file(s) ({format_size(selected_size)}) to sync.")

    if args.dry_run:
        print("Mode: DRY RUN (no files will be modified)\n")
    else:
        print()

    summary_rows = []
    synced_count = 0
    skipped_count = 0
    failed_count = 0
    total_bytes_synced = 0
    overall_start = time.monotonic()

    for idx, (video_file, rel_name) in enumerate(video_entries, start=1):
        file_size = video_file.stat().st_size
        print(f"[{idx}/{len(video_entries)}] Syncing: {rel_name} ({format_size(file_size)})")

        try:
            status, speed_str, elapsed, transferred = sync_single_file(
                source_dir=video_file.parent,
                video_file=video_file,
                destination=destination,
                ssh_cmd=ssh_cmd,
                dry_run=args.dry_run,
                remove_source_files=args.remove_source_files
            )
            if transferred:
                synced_count += 1
                total_bytes_synced += file_size
            else:
                skipped_count += 1

            summary_rows.append([
                rel_name,
                format_size(file_size),
                speed_str,
                format_time(elapsed),
                status
            ])
        except Exception as e:
            failed_count += 1
            print(f"  [!] Error syncing {rel_name}: {e}")
            summary_rows.append([
                rel_name,
                format_size(file_size),
                "-",
                "-",
                "Failed"
            ])

    overall_elapsed = time.monotonic() - overall_start
    print_summary_table(
        results=summary_rows,
        total_files=len(video_entries),
        synced_count=synced_count,
        skipped_count=skipped_count,
        failed_count=failed_count,
        total_bytes_synced=total_bytes_synced,
        total_elapsed=overall_elapsed,
        destination=destination
    )

    if failed_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
