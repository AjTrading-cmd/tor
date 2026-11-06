#!/usr/bin/env python3
import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time

RSYNC_LOCK_FILE = Path("/tmp/tor_rsync_transfer_queue.lock")
RSYNC_STATUS_FILE = Path("/tmp/tor_rsync_transfer_status.json")
PROGRESS_RE = re.compile(r"^\s*(\d+)\s+(\d+)%\s+(\S+/s)\s+(\S+)")
VIDEO_EXTENSIONS = {".mkv", ".mp4"}
IGNORE_SUFFIXES = (
    ".!qb",
    ".part",
    ".tmp",
    ".crdownload",
    ".partial",
    ".temp_stereo.mkv",
)

# Regex patterns to identify TV Shows vs Movies from filenames / parent paths
TV_PATTERNS = [
    # Standard S01E02, S01.E02, S01_E02, S01E02E03, S1E2
    re.compile(r"(?<![a-zA-Z0-9])[sS]\d{1,2}[\.\-_\s]?[eE]\d{1,3}(?![a-zA-Z0-9])"),
    # 1x02, 01x02
    re.compile(r"(?<![a-zA-Z0-9])\d{1,2}[xX]\d{2,3}(?![a-zA-Z0-9])"),
    # Season 1, Season 01, S01 (standalone season folder/tag)
    re.compile(r"(?<![a-zA-Z0-9])(?:[sS]eason[\.\-_\s]?\d{1,2}|[sS]\d{2})(?![a-zA-Z0-9])"),
    # Episode 01, Ep 01, Ep.01, E01
    re.compile(r"(?<![a-zA-Z0-9])(?:[eE]pisode|[eE]p)[\.\-_\s]?\d{1,3}(?![a-zA-Z0-9])"),
    # Miniseries Part 1 / Pt. 1
    re.compile(r"(?<![a-zA-Z0-9])(?:[pP]art|[pP]t)[\.\-_\s]?\d{1,2}(?![a-zA-Z0-9])"),
    # Daily / dated shows: 2026.11.05 or 2026-11-05
    re.compile(r"(?<![a-zA-Z0-9])(19|20)\d{2}[\.\-_](0[1-9]|1[0-2])[\.\-_](0[1-9]|[12]\d|3[01])(?![a-zA-Z0-9])"),
]


def classify_media_type(video_file: Path) -> str:
    """
    Determines whether a video file (.mkv/.mp4) is a 'tv' show or 'movie'
    using filename/folder regex patterns and optional ffprobe container metadata.
    Returns 'tv' or 'movie'.
    """
    # Check filename and immediate parent folder name (unless parent is a generic bucket)
    generic_folders = {"safe_files", "skeptical_files", "incoming", "downloads", "movies", "tv", "tv_shows", "media"}
    candidates = [video_file.name]
    if video_file.parent.name.lower() not in generic_folders:
        candidates.append(video_file.parent.name)

    for text in candidates:
        for pat in TV_PATTERNS:
            if pat.search(text):
                return "tv"

    # Secondary check: inspect container tags via ffprobe if available
    if shutil.which("ffprobe"):
        cmd = [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            str(video_file),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            try:
                data = json.loads(res.stdout)
                tags = data.get("format", {}).get("tags", {}) or {}
                tag_keys_lower = {k.lower(): str(v) for k, v in tags.items()}
                if any(k in tag_keys_lower for k in ("show", "season_number", "episode_id", "episode_sort", "tvshow")):
                    return "tv"
                title_val = tag_keys_lower.get("title", "")
                if title_val and any(pat.search(title_val) for pat in TV_PATTERNS):
                    return "tv"
            except Exception:
                pass

    return "movie"


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


def update_rsync_status(**kwargs):
    """Writes live transfer progress/state to `/tmp/tor_rsync_transfer_status.json` for the status UI."""
    try:
        current = {}
        if RSYNC_STATUS_FILE.exists():
            try:
                current = json.loads(RSYNC_STATUS_FILE.read_text(encoding="utf-8"))
            except Exception:
                current = {}
        current.update(kwargs)
        current["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        current["pid"] = os.getpid()
        tmp_path = RSYNC_STATUS_FILE.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(current, indent=2), encoding="utf-8")
        tmp_path.replace(RSYNC_STATUS_FILE)
    except Exception:
        pass


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
    update_rsync_status(
        state="TRANSFERRING" if progress < 1.0 else "IDLE",
        current_file=filename,
        progress_pct=round(percent, 1),
        speed=speed_str or "-",
        elapsed=format_time(elapsed),
        eta=eta_str,
    )


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
    cmd.extend([str(video_file), f"{destination.rstrip('/')}/"])

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
        assert process.stdout is not None
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
            assert process.stderr is not None
            err_msg = process.stderr.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(err_msg or f"rsync exited with code {process.returncode}")

        # If file was already up to date on remote and --remove-source-files is set,
        # rsync also removes the local file when it matches remote, or we ensure it's cleaned up
        if not dry_run and remove_source_files and video_file.exists():
            try:
                video_file.unlink()
            except OSError:
                pass

        if dry_run:
            status = "Dry Run (Would Sync)" if itemized_transfer else "Dry Run (Up to Date)"
        elif itemized_transfer:
            status = "Synced & Removed Local" if remove_source_files else "Synced"
        else:
            status = "Up to Date (Removed Local)" if remove_source_files else "Skipped (Up to Date)"

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
    headers = ["File Name", "Type", "Size", "Avg Speed", "Time Elapsed", "Remote Destination", "Action / Status"]

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
        mtype = "TV Show" if classify_media_type(vf) == "tv" else "Movie"
        print(f"  [{idx}] [{mtype}] {rel_name} ({size_str})")

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


def wait_until_file_stable(file_path: Path, stability_seconds: float = 1.5, timeout: float = 300.0) -> bool:
    """Waits until a file's size and mtime stop changing before rsyncing."""
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


def resolve_remote_dest_for_file(
    video_file: Path,
    remote_host: str,
    default_remote_path: str,
    tv_remote_path: str | None,
) -> tuple[str, str]:
    """
    Determines the media type ('TV Show' or 'Movie') and returns
    (media_label, full_remote_destination_string).
    If `tv_remote_path` is provided, TV shows go to `remote_host:tv_remote_path`
    and Movies go to `remote_host:default_remote_path`.
    If `tv_remote_path` is not explicitly provided, checks if `default_remote_path`
    ends with `/movies` and automatically routes TV shows to the sibling `/tv_shows`
    (or uses `default_remote_path/movies` and `default_remote_path/tv_shows` when
    `--auto-split-dirs` is active).
    """
    mtype = classify_media_type(video_file)
    label = "TV Show" if mtype == "tv" else "Movie"

    if mtype == "tv" and tv_remote_path:
        return label, f"{remote_host}:{tv_remote_path}"
    return label, f"{remote_host}:{default_remote_path}"


@contextmanager
def acquire_rsync_transfer_lock():
    """
    Ensures only ONE rsync transfer runs at a time across the system,
    while using a separate lock file from `watch_and_scan_media.py` so malware scans
    and rsync transfers can run concurrently with each other.
    """
    lock_fd = open(RSYNC_LOCK_FILE, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(
                f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                f"[Rsync Queue] Another rsync transfer is currently active (PID {os.getpid()} waiting in queue)..."
            )
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
            print(
                f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                f"[Rsync Queue] Lock acquired by PID {os.getpid()}; starting rsync."
            )
        yield
    finally:
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
        finally:
            lock_fd.close()


def ensure_remote_dir_exists(ssh_cmd: list[str], remote_host: str, remote_path: str, dry_run: bool):
    """Ensures the destination directory exists on the remote server via `mkdir -p`."""
    if dry_run:
        return
    cmd = ssh_cmd + [remote_host, f"mkdir -p {shlex.quote(remote_path)}"]
    subprocess.run(cmd, capture_output=True, text=True)


def watch_and_sync_directory(
    watch_dir: Path,
    remote_host: str,
    movies_remote_path: str,
    tv_remote_path: str,
    ssh_cmd: list[str],
    dry_run: bool,
    remove_source_files: bool,
    recursive: bool = True,
    poll_interval: float = 3.0,
):
    """
    Continuously watches `watch_dir` (e.g. `safe_files/`) using `inotifywait`
    (with polling fallback) and an internal FIFO worker queue + cross-process lock.
    Whenever a Movie or TV Show `.mkv`/`.mp4` lands in `watch_dir`, it is enqueued
    immediately and synced strictly one at a time.
    """
    watch_dir.mkdir(parents=True, exist_ok=True)
    ensure_remote_dir_exists(ssh_cmd, remote_host, movies_remote_path, dry_run)
    if tv_remote_path != movies_remote_path:
        ensure_remote_dir_exists(ssh_cmd, remote_host, tv_remote_path, dry_run)

    print("=" * 75)
    print(f" RSYNC WATCHDOG ACTIVE ON: {watch_dir}")
    print(f"   Movies Destination   -> {remote_host}:{movies_remote_path}")
    print(f"   TV Shows Destination -> {remote_host}:{tv_remote_path}")
    print(f"   Remove Local After   -> {'Yes' if remove_source_files else 'No'}")
    print(f"   Concurrency Queue    -> 1 transfer at a time (FIFO)")
    print("=" * 75)
    print("Press Ctrl+C to stop.\n")

    transfer_queue: queue.Queue[Path] = queue.Queue()
    queued_or_active: set[Path] = set()
    state_lock = threading.Lock()
    stop_event = threading.Event()

    def enqueue_candidate(vf: Path):
        resolved = vf.resolve()
        if not is_eligible_video_file(resolved):
            return
        with state_lock:
            if resolved in queued_or_active:
                return
            queued_or_active.add(resolved)
            q_size = transfer_queue.qsize() + 1
        if q_size > 1:
            print(f"  [Rsync Queue] Enqueued {resolved.name} (position #{q_size} in transfer queue)")
        transfer_queue.put(resolved)
        update_rsync_status(queued_count=transfer_queue.qsize(), watch_dir=str(watch_dir))

    def process_candidate(vf: Path):
        if not is_eligible_video_file(vf):
            return
        if not wait_until_file_stable(vf, stability_seconds=1.5):
            return
        if not vf.exists():
            return

        with acquire_rsync_transfer_lock():
            if not vf.exists():
                return
            label, full_dest = resolve_remote_dest_for_file(
                vf, remote_host, movies_remote_path, tv_remote_path
            )
            file_size = vf.stat().st_size
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{now_str}] Syncing [{label}]: {vf.name} ({format_size(file_size)}) -> {full_dest}")
            update_rsync_status(
                state="TRANSFERRING",
                current_file=vf.name,
                media_type=label,
                file_size=format_size(file_size),
                destination=full_dest,
                progress_pct=0.0,
            )
            try:
                status, speed_str, elapsed, _ = sync_single_file(
                    source_dir=vf.parent,
                    video_file=vf,
                    destination=full_dest,
                    ssh_cmd=ssh_cmd,
                    dry_run=dry_run,
                    remove_source_files=remove_source_files,
                )
                print(f"  -> {status} in {format_time(elapsed)} ({speed_str})\n")
                update_rsync_status(
                    state="IDLE",
                    current_file="",
                    progress_pct=0.0,
                    last_file=vf.name,
                    last_type=label,
                    last_destination=full_dest,
                    last_status=status,
                    last_finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                )
            except Exception as exc:
                print(f"  [!] Failed to sync {vf.name}: {exc}\n")
                update_rsync_status(
                    state="IDLE",
                    current_file="",
                    last_file=vf.name,
                    last_status=f"Failed: {exc}",
                    last_finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                )

    def worker_loop():
        while not stop_event.is_set():
            try:
                vf = transfer_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            update_rsync_status(queued_count=transfer_queue.qsize())
            try:
                process_candidate(vf)
            finally:
                with state_lock:
                    queued_or_active.discard(vf)
                transfer_queue.task_done()
                update_rsync_status(queued_count=transfer_queue.qsize())

    worker = threading.Thread(target=worker_loop, daemon=True)
    worker.start()

    def sweep_existing():
        glob_func = watch_dir.rglob if recursive else watch_dir.glob
        for f in sorted(glob_func("*")):
            if is_eligible_video_file(f):
                enqueue_candidate(f)

    # Initial sweep of anything already sitting in safe_files
    sweep_existing()

    if shutil.which("inotifywait"):
        print("Listening for new files via inotifywait + FIFO queue...")
        cmd = [
            "inotifywait",
            "-m",
            "-q",
        ]
        if recursive:
            cmd.append("-r")
        cmd.extend([
            "-e", "close_write,moved_to",
            "--format", "%w%f",
            str(watch_dir),
        ])
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                candidate = Path(line.strip())
                if is_eligible_video_file(candidate):
                    enqueue_candidate(candidate)
        except KeyboardInterrupt:
            print("\nStopping rsync watchdog...")
        finally:
            stop_event.set()
            proc.terminate()
    else:
        print(f"Polling {watch_dir} every {poll_interval}s (install 'inotify-tools' for instant events)...")
        try:
            while True:
                sweep_existing()
                time.sleep(poll_interval)
        except KeyboardInterrupt:
            print("\nStopping rsync watchdog...")
        finally:
            stop_event.set()


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Rsync .mkv and .mp4 video files from local file(s) or directory(ies) (such as safe_files/) "
            "to a remote server, with optional watchdog mode and automatic Movie vs TV Show routing."
        )
    )
    parser.add_argument(
        "source_dir",
        type=str,
        help="Path to a local directory (e.g. /path/to/safe_files) or .mkv/.mp4 file to sync"
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
        help="Destination directory on the remote server for Movies (or base media dir). "
             "Can also be provided directly in remote_server as user@host:/path/to/remote/movies"
    )
    parser.add_argument(
        "-w", "--watch",
        action="store_true",
        help="Run continuously as a watchdog on source_dir (e.g., safe_files/), syncing new files as they arrive "
             "and removing local copies after transfer by default"
    )
    parser.add_argument(
        "--tv-dir",
        type=str,
        default=None,
        help="Remote directory for TV Shows (e.g. /path/to/remote/tv_shows). If omitted when --watch is used, "
             "automatically derives a sibling 'tv_shows' folder if remote_dir is named 'movies', or creates "
             "'movies/' and 'tv_shows/' subfolders inside remote_dir."
    )
    parser.add_argument(
        "--same-remote-dir",
        action="store_true",
        help="Send both Movies and TV Shows into the exact same remote_dir without splitting into movies/ and tv_shows/"
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
        default=None,
        help="Remove local .mkv/.mp4 files after they are successfully transferred (enabled by default in --watch mode)"
    )
    parser.add_argument(
        "--keep-source-files",
        action="store_true",
        help="Keep local files in source_dir even when running in --watch mode"
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
        "Example: ./rsync_mkv_to_remote.py /local/safe_files user@debian-host /remote/media --watch\n"
        "     or: ./rsync_mkv_to_remote.py /local/safe_files user@debian-host:/remote/movies --tv-dir /remote/tv_shows --watch"
    )
    sys.exit(1)


def compute_movies_and_tv_paths(
    remote_path: str,
    tv_dir_arg: str | None,
    same_remote_dir: bool,
    auto_split_default: bool,
) -> tuple[str, str]:
    """
    Computes `(movies_remote_path, tv_remote_path)`:
    - If `--same-remote-dir` is passed: both return `remote_path`.
    - If `--tv-dir` is explicitly passed: movies -> `remote_path`, TV -> `tv_dir_arg`.
    - If `auto_split_default` is True (e.g. in `--watch` mode):
      * If `remote_path` ends with `/movies` (case-insensitive), TV goes to sibling `/tv_shows`.
      * Otherwise, `remote_path` is treated as the base media directory and routes to
        `<remote_path>/movies` and `<remote_path>/tv_shows`.
    - Otherwise (standard one-shot run without `--tv-dir`), both default to `remote_path`
      unless `--tv-dir` is provided.
    """
    clean_base = remote_path.rstrip("/") or "/"
    if same_remote_dir:
        return clean_base, clean_base
    if tv_dir_arg:
        return clean_base, tv_dir_arg.rstrip("/") or "/"
    if auto_split_default:
        base_name = Path(clean_base).name.lower()
        if base_name in ("movies", "movie", "films"):
            parent = str(Path(clean_base).parent)
            tv_sibling = f"{parent.rstrip('/')}/tv_shows"
            return clean_base, tv_sibling
        return f"{clean_base}/movies", f"{clean_base}/tv_shows"
    return clean_base, clean_base


def main():
    args = parse_args()

    if not shutil.which("rsync") or not shutil.which("ssh"):
        print("Error: 'rsync' and 'ssh' must be installed. Run: sudo apt install rsync openssh-client")
        sys.exit(1)

    local_sources, remote_host, remote_path = resolve_positional_args(args)

    # Determine whether local source files should be removed after sync
    if args.keep_source_files:
        remove_source = False
    elif args.remove_source_files is not None:
        remove_source = args.remove_source_files
    else:
        # In --watch mode, remove local files from safe_files after successful sync by default
        remove_source = bool(args.watch)

    movies_remote_path, tv_remote_path = compute_movies_and_tv_paths(
        remote_path=remote_path,
        tv_dir_arg=args.tv_dir,
        same_remote_dir=args.same_remote_dir,
        auto_split_default=bool(args.watch),
    )

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

    # Watchdog mode
    if args.watch:
        watch_dir = Path(local_sources[0]).expanduser().resolve()
        watch_and_sync_directory(
            watch_dir=watch_dir,
            remote_host=remote_host,
            movies_remote_path=movies_remote_path,
            tv_remote_path=tv_remote_path,
            ssh_cmd=ssh_cmd,
            dry_run=args.dry_run,
            remove_source_files=remove_source,
            recursive=args.recursive,
        )
        sys.exit(0)

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
                if is_eligible_video_file(f)
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

    ensure_remote_dir_exists(ssh_cmd, remote_host, movies_remote_path, args.dry_run)
    if tv_remote_path != movies_remote_path:
        ensure_remote_dir_exists(ssh_cmd, remote_host, tv_remote_path, args.dry_run)

    destination_summary = (
        f"{remote_host}:{movies_remote_path} (Movies) | {remote_host}:{tv_remote_path} (TV Shows)"
        if tv_remote_path != movies_remote_path
        else f"{remote_host}:{movies_remote_path}"
    )
    total_size = sum(f.stat().st_size for f, _ in video_entries)

    print(f"Found {len(video_entries)} video file(s) (.mkv/.mp4, {format_size(total_size)})")
    print(f"Destination: {destination_summary}")

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
        with acquire_rsync_transfer_lock():
            if not video_file.exists():
                continue
            file_size = video_file.stat().st_size
            label, file_dest = resolve_remote_dest_for_file(
                video_file, remote_host, movies_remote_path, tv_remote_path
            )
            print(f"[{idx}/{len(video_entries)}] Syncing [{label}]: {rel_name} ({format_size(file_size)}) -> {file_dest}")

            try:
                status, speed_str, elapsed, transferred = sync_single_file(
                    source_dir=video_file.parent,
                    video_file=video_file,
                    destination=file_dest,
                    ssh_cmd=ssh_cmd,
                    dry_run=args.dry_run,
                    remove_source_files=remove_source
                )
                if transferred:
                    synced_count += 1
                    total_bytes_synced += file_size
                else:
                    skipped_count += 1

                summary_rows.append([
                    rel_name,
                    label,
                    format_size(file_size),
                    speed_str,
                    format_time(elapsed),
                    file_dest,
                    status
                ])
            except Exception as e:
                failed_count += 1
                print(f"  [!] Error syncing {rel_name}: {e}")
                summary_rows.append([
                    rel_name,
                    label,
                    format_size(file_size),
                    "-",
                    "-",
                    file_dest,
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
        destination=destination_summary
    )

    if failed_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
