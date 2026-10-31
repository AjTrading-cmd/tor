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
    mkv_file: Path,
    destination: str,
    ssh_cmd: list[str],
    dry_run: bool,
    remove_source_files: bool
) -> tuple[str, str, float, bool]:
    """
    Rsyncs a single .mkv file while rendering a live progress bar with Elapsed & ETA.
    Returns (status_str, last_speed_str, elapsed_seconds, was_transferred).
    """
    rel_path = mkv_file.relative_to(source_dir)
    display_name = str(rel_path)
    file_size = mkv_file.stat().st_size

    cmd = [
        "rsync",
        "-avR",
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

    # Using /./ with -R preserves relative subdirectory paths inside source_dir
    source_arg = f"{source_dir}/./{rel_path}"
    cmd.extend([source_arg, f"{destination}/"])

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


def parse_args():
    parser = argparse.ArgumentParser(
        description="Rsync all .mkv files from a local directory to a directory on a remote server."
    )
    parser.add_argument(
        "source_dir",
        type=str,
        help="Path to the local directory containing .mkv files"
    )
    parser.add_argument(
        "remote_server",
        type=str,
        help="Remote server address (e.g., user@192.168.1.100 or user@debian-server.local)"
    )
    parser.add_argument(
        "remote_dir",
        type=str,
        nargs="?",
        default=None,
        help="Destination directory on the remote server (e.g., /path/to/remote/movies). "
             "Can also be provided directly in remote_server as user@host:/path/to/remote/movies"
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
        help="Recursively find and sync .mkv files in subdirectories"
    )
    parser.add_argument(
        "-n", "--dry-run",
        action="store_true",
        help="Perform a trial run with no changes made"
    )
    parser.add_argument(
        "--remove-source-files",
        action="store_true",
        help="Remove local .mkv files after they are successfully transferred"
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if not shutil.which("rsync") or not shutil.which("ssh"):
        print("Error: 'rsync' and 'ssh' must be installed. Run: sudo apt install rsync openssh-client")
        sys.exit(1)

    source_dir = Path(args.source_dir).expanduser().resolve()
    if not source_dir.is_dir():
        print(f"Error: Local directory not found: {source_dir}")
        sys.exit(1)

    # Support either `user@host /remote/path` or `user@host:/remote/path`
    if args.remote_dir:
        remote_host = args.remote_server.rstrip(":")
        remote_path = args.remote_dir
    elif ":" in args.remote_server:
        remote_host, remote_path = args.remote_server.split(":", 1)
        if not remote_path:
            print("Error: Remote directory path cannot be empty.")
            sys.exit(1)
    else:
        print(
            "Error: Please specify the destination directory on the remote server.\n"
            "Example: ./rsync_mkv_to_remote.py /local/movies user@debian-host /remote/movies"
        )
        sys.exit(1)

    glob_func = source_dir.rglob if args.recursive else source_dir.glob
    mkv_files = sorted([
        f for f in glob_func("*.mkv")
        if f.is_file() and not f.name.endswith(".temp_stereo.mkv")
    ])

    if not mkv_files:
        print(f"No .mkv files found in '{source_dir}'")
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
    total_size = sum(f.stat().st_size for f in mkv_files)

    print(f"Found {len(mkv_files)} .mkv file(s) ({format_size(total_size)}) in '{source_dir}'")
    print(f"Destination: {destination}")
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

    for idx, mkv_file in enumerate(mkv_files, start=1):
        rel_name = str(mkv_file.relative_to(source_dir))
        file_size = mkv_file.stat().st_size
        print(f"[{idx}/{len(mkv_files)}] Syncing: {rel_name} ({format_size(file_size)})")

        try:
            status, speed_str, elapsed, transferred = sync_single_file(
                source_dir=source_dir,
                mkv_file=mkv_file,
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
        total_files=len(mkv_files),
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
