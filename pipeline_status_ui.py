#!/usr/bin/env python3
"""
pipeline_status_ui.py

Renders a live, colorized ASCII/Unicode terminal dashboard showing the current
real-time state, queues, locks, active processes, progress bars, and recent
monthly scan logs for:
  1. `watch_and_scan_media.py` (Malware Scanner & Season Splitter)
  2. `split_season_video.py`   (Full-Season Episode Splitter)
  3. `rsync_mkv_to_remote.py`  (Remote Rsync Watchdog & Movie/TV Router)

Usage:
  ./pipeline_status_ui.py          # Live auto-refreshing dashboard (every 1s)
  ./pipeline_status_ui.py --once   # Print snapshot once and exit
"""

import argparse
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

REPO_ROOT = Path(__file__).resolve().parent
DOCS_DIR = REPO_ROOT / "docs"

SCAN_LOCK_FILE = Path("/tmp/tor_malware_scan_queue.lock")
SCAN_STATUS_FILE = Path("/tmp/tor_malware_scan_status.json")

RSYNC_LOCK_FILE = Path("/tmp/tor_rsync_transfer_queue.lock")
RSYNC_STATUS_FILE = Path("/tmp/tor_rsync_transfer_status.json")

# ANSI escape codes for styling
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[38;5;82m"
YELLOW = "\033[38;5;220m"
RED = "\033[38;5;196m"
CYAN = "\033[38;5;51m"
BLUE = "\033[38;5;39m"
MAGENTA = "\033[38;5;213m"
WHITE = "\033[38;5;255m"
GRAY = "\033[38;5;245m"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_PREV_CPU_TIMES: tuple[int, int] | None = None


def read_cpu_stat() -> tuple[int, int]:
    """Reads `/proc/stat` and returns `(idle_time, total_time)` across all CPU cores."""
    try:
        with open("/proc/stat", "r", encoding="utf-8") as f:
            first_line = f.readline()
        parts = [int(x) for x in first_line.split()[1:]]
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        total = sum(parts)
        return idle, total
    except Exception:
        return 0, 0


def get_system_metrics() -> dict:
    """
    Collects live system hardware & OS metrics (`top`-style):
    - Total CPU usage % (delta across `/proc/stat`)
    - Load average (1m, 5m, 15m) & CPU core count
    - RAM usage (used / total GiB & %) and Swap usage
    - CPU Package & Core temperatures (`x86_pkg_temp` / `coretemp` / `hwmon`)
    - Disk usage for the workspace filesystem
    - System uptime
    """
    global _PREV_CPU_TIMES
    idle1, total1 = read_cpu_stat()
    if _PREV_CPU_TIMES is None:
        time.sleep(0.08)
        idle2, total2 = read_cpu_stat()
    else:
        idle2, total2 = idle1, total1
        idle1, total1 = _PREV_CPU_TIMES
    _PREV_CPU_TIMES = (idle2, total2)

    delta_total = max(1, total2 - total1)
    delta_idle = max(0, idle2 - idle1)
    cpu_pct = max(0.0, min(100.0, (1.0 - (delta_idle / delta_total)) * 100.0))

    try:
        load1, load5, load15 = os.getloadavg()
    except OSError:
        load1 = load5 = load15 = 0.0
    cpu_cores = os.cpu_count() or 1

    # Memory & Swap from /proc/meminfo
    mem_info: dict[str, int] = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if ":" in line:
                    k, v = line.split(":", 1)
                    val_parts = v.strip().split()
                    if val_parts and val_parts[0].isdigit():
                        mem_info[k.strip()] = int(val_parts[0]) * 1024  # kB -> bytes
    except Exception:
        pass

    mem_total = mem_info.get("MemTotal", 0)
    mem_avail = mem_info.get("MemAvailable", mem_info.get("MemFree", 0))
    mem_used = max(0, mem_total - mem_avail)
    mem_pct = (mem_used / mem_total * 100.0) if mem_total > 0 else 0.0

    swap_total = mem_info.get("SwapTotal", 0)
    swap_free = mem_info.get("SwapFree", 0)
    swap_used = max(0, swap_total - swap_free)
    swap_pct = (swap_used / swap_total * 100.0) if swap_total > 0 else 0.0

    # CPU Temperature from /sys/class/thermal or /sys/class/hwmon
    cpu_temp_c: float | None = None
    try:
        thermal_base = Path("/sys/class/thermal")
        if thermal_base.exists():
            preferred_zones = []
            fallback_zones = []
            for tz in thermal_base.glob("thermal_zone*"):
                t_type = (tz / "type").read_text(encoding="utf-8").strip().lower()
                t_val = int((tz / "temp").read_text(encoding="utf-8").strip()) / 1000.0
                if t_val > 0:
                    if any(k in t_type for k in ("x86_pkg", "pkg", "cpu", "core", "k10temp", "tctl")):
                        preferred_zones.append(t_val)
                    else:
                        fallback_zones.append(t_val)
            if preferred_zones:
                cpu_temp_c = max(preferred_zones)
            elif fallback_zones:
                cpu_temp_c = max(fallback_zones)
    except Exception:
        pass

    # Disk space on workspace partition
    try:
        du = shutil.disk_usage(REPO_ROOT)
        disk_used_gb = du.used / (1024 ** 3)
        disk_total_gb = du.total / (1024 ** 3)
        disk_pct = (du.used / du.total * 100.0) if du.total > 0 else 0.0
    except Exception:
        disk_used_gb = disk_total_gb = disk_pct = 0.0

    # Uptime from /proc/uptime
    uptime_str = "-"
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as f:
            up_secs = int(float(f.read().split()[0]))
        days, rem = divmod(up_secs, 86400)
        hours, rem = divmod(rem, 3600)
        mins, _ = divmod(rem, 60)
        uptime_str = f"{days}d {hours:02d}h {mins:02d}m" if days > 0 else f"{hours:02d}h {mins:02d}m"
    except Exception:
        pass

    return {
        "cpu_pct": cpu_pct,
        "cpu_cores": cpu_cores,
        "load": (load1, load5, load15),
        "mem_used_gb": mem_used / (1024 ** 3),
        "mem_total_gb": mem_total / (1024 ** 3),
        "mem_pct": mem_pct,
        "swap_used_gb": swap_used / (1024 ** 3),
        "swap_total_gb": swap_total / (1024 ** 3),
        "swap_pct": swap_pct,
        "temp_c": cpu_temp_c,
        "disk_used_gb": disk_used_gb,
        "disk_total_gb": disk_total_gb,
        "disk_pct": disk_pct,
        "uptime": uptime_str,
    }


def make_mini_gauge(pct: float, width: int = 12) -> str:
    """Renders a compact color-coded bar gauge (Green -> Yellow -> Red) for CPU/RAM/Disk."""
    pct = min(max(float(pct), 0.0), 100.0)
    filled = int(round((pct / 100.0) * width))
    bar = "█" * filled + "░" * (width - filled)
    if pct >= 85.0:
        color = RED
    elif pct >= 65.0:
        color = YELLOW
    else:
        color = GREEN
    return f"{color}[{bar}]{RESET} {BOLD}{pct:5.1f}%{RESET}"


def visible_len(text: str) -> int:
    """Returns printable character length ignoring ANSI color codes."""
    return len(ANSI_RE.sub("", text))


def pad_ansi(text: str, width: int) -> str:
    """Pads or truncates a string containing ANSI codes to fit exactly `width` visible characters."""
    vlen = visible_len(text)
    if vlen <= width:
        return text + (" " * (width - vlen))
    # Truncate visible characters while preserving ANSI codes
    out = []
    seen = 0
    i = 0
    target = max(0, width - 3)
    while i < len(text) and seen < target:
        m = ANSI_RE.match(text, i)
        if m:
            out.append(m.group(0))
            i = m.end()
        else:
            out.append(text[i])
            seen += 1
            i += 1
    return "".join(out) + "..." + RESET + (" " * max(0, width - seen - 3))


def truncate_plain(text: str, max_len: int) -> str:
    """Truncates a plain string with ellipsis if longer than `max_len`."""
    text = str(text)
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    return text[: max_len - 3] + "..."


def is_lock_held(lock_path: Path) -> bool:
    """Checks non-blockingly whether an flock lock file is currently held by a process."""
    if not lock_path.exists():
        return False
    try:
        fd = open(lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            return False
        except BlockingIOError:
            return True
        finally:
            fd.close()
    except Exception:
        return False


def read_json_status(path: Path) -> dict:
    """Safely reads a JSON status file from `/tmp`."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def is_pid_alive(pid: int | None) -> bool:
    """Returns True if a PID exists and is running."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def get_running_scripts() -> dict[str, list[dict]]:
    """
    Scans running system processes for `watch_and_scan_media.py`, `split_season_video.py`,
    `rsync_mkv_to_remote.py`, `clamscan`/`clamdscan`, `ffmpeg`, and `rsync`.
    """
    categories: dict[str, list[dict]] = {
        "malware": [],
        "splitter": [],
        "rsync": [],
        "subtools": [],
    }
    try:
        res = subprocess.run(
            ["ps", "-eo", "pid,etime,pcpu,pmem,args"],
            capture_output=True,
            text=True,
        )
        for line in res.stdout.splitlines()[1:]:
            parts = line.strip().split(None, 4)
            if len(parts) < 5:
                continue
            pid_s, etime, pcpu, pmem, cmd = parts
            if not pid_s.isdigit():
                continue
            pid = int(pid_s)
            if pid == os.getpid() or "pipeline_status_ui.py" in cmd:
                continue

            entry = {
                "pid": pid,
                "etime": etime,
                "cpu": f"{pcpu}%",
                "mem": f"{pmem}%",
                "cmd": cmd,
            }
            if "watch_and_scan_media.py" in cmd:
                categories["malware"].append(entry)
            elif "split_season_video.py" in cmd:
                categories["splitter"].append(entry)
            elif "rsync_mkv_to_remote.py" in cmd:
                categories["rsync"].append(entry)
            elif any(bin_name in cmd for bin_name in ("clamscan", "clamdscan", "ffmpeg", "rsync -av")):
                if "grep" not in cmd:
                    categories["subtools"].append(entry)
    except Exception:
        pass
    return categories


def make_progress_bar(pct: float, width: int = 26) -> str:
    """Builds a colored ASCII/Unicode progress bar."""
    pct = min(max(float(pct), 0.0), 100.0)
    filled = int(round((pct / 100.0) * width))
    bar_str = "█" * filled + "░" * (width - filled)
    color = GREEN if pct >= 100.0 else CYAN
    return f"{color}[{bar_str}]{RESET} {BOLD}{pct:5.1f}%{RESET}"


def get_recent_docs_entries(max_rows: int = 5) -> tuple[str, int, int, int, list[list[str]]]:
    """
    Reads the latest monthly `docs/YYYY-MM.md` file to display summary counts and
    the most recently scanned files.
    Returns `(month_name, total_count, safe_count, skeptical_count, recent_rows)`.
    """
    if not DOCS_DIR.exists():
        return "None", 0, 0, 0, []

    month_files = sorted(
        [f for f in DOCS_DIR.iterdir() if f.is_file() and re.match(r"^\d{4}-\d{2}\.md$", f.name)],
        reverse=True,
    )
    if not month_files:
        return "None", 0, 0, 0, []

    latest = month_files[0]
    lines = latest.read_text(encoding="utf-8", errors="replace").splitlines()
    data_rows = [
        ln for ln in lines
        if ln.startswith("| ") and not ln.startswith("| Date / Time") and not ln.startswith("| :---")
    ]

    total = len(data_rows)
    safe = sum(1 for r in data_rows if "| SAFE |" in r)
    skeptical = sum(1 for r in data_rows if "| SKEPTICAL |" in r)

    parsed_recent: list[list[str]] = []
    for raw in reversed(data_rows[-max_rows:]):
        cells = [c.strip().strip("`") for c in raw.strip().strip("|").split("|")]
        if len(cells) >= 11:
            # Date, File Name, Status, Resolution, Safety Notes
            parsed_recent.append([
                cells[0],
                cells[1],
                cells[2],
                cells[7],
                cells[10],
            ])
    return latest.name, total, safe, skeptical, parsed_recent


def draw_box(title: str, lines: list[str], width: int, border_color: str = BLUE) -> str:
    """Draws a Unicode box with a title header and padded inner lines."""
    inner_w = width - 4
    title_clean = f" {title} "
    top_dash_len = max(0, width - 3 - visible_len(title_clean))
    out = [
        f"{border_color}╭─{ BOLD }{WHITE}{title_clean}{RESET}{border_color}{'─' * top_dash_len}╮{RESET}"
    ]
    for ln in lines:
        out.append(f"{border_color}│{RESET} {pad_ansi(ln, inner_w)} {border_color}│{RESET}")
    out.append(f"{border_color}╰{'─' * (width - 2)}╯{RESET}")
    return "\n".join(out)


def render_dashboard(watch_mode: bool = False) -> str:
    """Builds the full ASCII UI string."""
    term_cols = shutil.get_terminal_size((100, 35)).columns
    width = min(max(term_cols, 84), 116)
    inner_w = width - 4

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    procs = get_running_scripts()

    scan_locked = is_lock_held(SCAN_LOCK_FILE)
    rsync_locked = is_lock_held(RSYNC_LOCK_FILE)

    scan_status = read_json_status(SCAN_STATUS_FILE)
    rsync_status = read_json_status(RSYNC_STATUS_FILE)

    # Reset stale status if owning PID is no longer alive and lock is not held
    if scan_status.get("state") not in (None, "IDLE") and not scan_locked and not is_pid_alive(scan_status.get("pid")):
        scan_status["state"] = "IDLE"
        scan_status["stage"] = "Idle"
        scan_status["current_file"] = ""

    if rsync_status.get("state") not in (None, "IDLE") and not rsync_locked and not is_pid_alive(rsync_status.get("pid")):
        rsync_status["state"] = "IDLE"
        rsync_status["current_file"] = ""

    output_blocks: list[str] = []

    # 1. Top Banner + System Hardware Metrics (CPU, RAM, Temp, Load, Disk, Uptime)
    sys_m = get_system_metrics()
    header_title = f"{BOLD}{CYAN}⚡ TOR MEDIA PIPELINE DASHBOARD{RESET}"
    clock_str = f"{GRAY}Up: {WHITE}{sys_m['uptime']}{GRAY} • {WHITE}{now_str}{RESET}"
    spacing = max(1, inner_w - visible_len(header_title) - visible_len(clock_str))

    if sys_m["temp_c"] is not None:
        t_val = sys_m["temp_c"]
        t_color = RED if t_val >= 80.0 else (YELLOW if t_val >= 65.0 else GREEN)
        temp_str = f"{t_color}{BOLD}{t_val:.1f}°C{RESET}"
    else:
        temp_str = f"{GRAY}N/A{RESET}"

    l1, l5, l15 = sys_m["load"]
    load_str = f"{WHITE}{l1:.2f}{GRAY}, {l5:.2f}, {l15:.2f} ({sys_m['cpu_cores']}c){RESET}"
    ram_detail = f"{WHITE}{sys_m['mem_used_gb']:.1f}/{sys_m['mem_total_gb']:.1f}G{RESET}"
    disk_detail = f"{WHITE}{sys_m['disk_used_gb']:.0f}/{sys_m['disk_total_gb']:.0f}G ({sys_m['disk_pct']:.0f}%){RESET}"
    swap_detail = f"{WHITE}{sys_m['swap_used_gb']:.1f}/{sys_m['swap_total_gb']:.1f}G{RESET}"

    banner_lines = [
        f"{header_title}{' ' * spacing}{clock_str}",
        f"{DIM}{'─' * inner_w}{RESET}",
        f"{BOLD}CPU:{RESET} {make_mini_gauge(sys_m['cpu_pct'], 10)}  {BOLD}Temp:{RESET} {temp_str}  {BOLD}Load:{RESET} {load_str}",
        f"{BOLD}RAM:{RESET} {make_mini_gauge(sys_m['mem_pct'], 10)} {ram_detail}  {BOLD}Swap:{RESET} {swap_detail}  {BOLD}Disk:{RESET} {disk_detail}",
    ]
    output_blocks.append(draw_box("SYSTEM & HARDWARE METRICS (top)", banner_lines, width, border_color=CYAN))

    # 2. Malware Scanner & Season Splitter Box
    malware_procs = procs["malware"]
    splitter_procs = procs["splitter"]
    scan_state = scan_status.get("state", "IDLE")

    if scan_locked or scan_state not in ("IDLE", None):
        state_badge = f"{BOLD}{YELLOW}● ACTIVE ({scan_state}){RESET}"
    elif malware_procs:
        state_badge = f"{BOLD}{GREEN}● WATCHING (IDLE){RESET}"
    else:
        state_badge = f"{GRAY}○ STANDBY (qBittorrent/Cron){RESET}"

    waiting_scan_procs = max(0, len(malware_procs) - 1) if scan_locked else 0
    in_mem_q = int(scan_status.get("queued_count", 0) or 0)
    total_scan_queued = max(waiting_scan_procs, in_mem_q)

    lock_str = f"{YELLOW}LOCKED (1 active){RESET}" if scan_locked else f"{GREEN}UNLOCKED{RESET}"
    cur_file = truncate_plain(scan_status.get("current_file") or "None", inner_w - 22)
    cur_stage = truncate_plain(scan_status.get("stage") or "Waiting for incoming .mkv/.mp4 files", inner_w - 22)
    last_file = truncate_plain(scan_status.get("last_file") or "None yet", inner_w - 38)
    last_res = scan_status.get("last_result") or "-"
    last_res_colored = (
        f"{GREEN}{last_res}{RESET}" if "SAFE" in last_res
        else (f"{RED}{last_res}{RESET}" if "SKEPTICAL" in last_res or "INFECTED" in last_res else f"{GRAY}{last_res}{RESET}")
    )
    last_time = scan_status.get("last_finished_at") or "-"

    scan_lines = [
        f"{BOLD}Status:{RESET}        {state_badge}   {BOLD}Lock:{RESET} {lock_str}   {BOLD}Queue:{RESET} {CYAN}{total_scan_queued} waiting{RESET}",
        f"{BOLD}Active File:{RESET}   {WHITE}{cur_file}{RESET} {GRAY}({scan_status.get('file_size', '-')}){RESET}",
        f"{BOLD}Current Stage:{RESET} {MAGENTA}{cur_stage}{RESET}",
        f"{BOLD}Last Scanned:{RESET}  {last_file}  [{last_res_colored}] {GRAY}@ {last_time}{RESET}",
    ]
    if malware_procs or splitter_procs:
        proc_strs = [
            f"PID {p['pid']} ({p['etime']}, CPU {p['cpu']})"
            for p in (malware_procs + splitter_procs)[:3]
        ]
        scan_lines.append(f"{BOLD}Processes:{RESET}     {GREEN}{', '.join(proc_strs)}{RESET}")
    else:
        scan_lines.append(f"{BOLD}Processes:{RESET}     {GRAY}No active scan processes right now{RESET}")

    output_blocks.append(
        draw_box(
            "1. MALWARE SCANNER & SEASON SPLITTER (watch_and_scan_media.py)",
            scan_lines,
            width,
            border_color=MAGENTA if (scan_locked or scan_state != "IDLE") else BLUE,
        )
    )

    # 3. Rsync Remote Watchdog Box
    rsync_procs = procs["rsync"]
    r_state = rsync_status.get("state", "IDLE")

    if rsync_locked or r_state == "TRANSFERRING":
        r_badge = f"{BOLD}{CYAN}● TRANSFERRING{RESET}"
    elif rsync_procs:
        r_badge = f"{BOLD}{GREEN}● WATCHDOG ACTIVE (IDLE){RESET}"
    else:
        r_badge = f"{GRAY}○ OFFLINE (Not running){RESET}"

    waiting_rsync_procs = max(0, len(rsync_procs) - 1) if rsync_locked else 0
    r_in_mem_q = int(rsync_status.get("queued_count", 0) or 0)
    total_rsync_queued = max(waiting_rsync_procs, r_in_mem_q)

    r_lock_str = f"{YELLOW}LOCKED (1 active){RESET}" if rsync_locked else f"{GREEN}UNLOCKED{RESET}"
    r_cur_file = truncate_plain(rsync_status.get("current_file") or "None", inner_w - 28)
    r_type = rsync_status.get("media_type") or "-"
    r_dest = truncate_plain(rsync_status.get("destination") or "-", inner_w - 22)
    r_pct = float(rsync_status.get("progress_pct", 0.0) or 0.0)
    r_speed = rsync_status.get("speed") or "-"
    r_eta = rsync_status.get("eta") or "--:--:--"

    r_last_file = truncate_plain(rsync_status.get("last_file") or "None yet", inner_w - 40)
    r_last_status = rsync_status.get("last_status") or "-"
    r_last_time = rsync_status.get("last_finished_at") or "-"

    rsync_lines = [
        f"{BOLD}Status:{RESET}        {r_badge}   {BOLD}Lock:{RESET} {r_lock_str}   {BOLD}Queue:{RESET} {CYAN}{total_rsync_queued} waiting{RESET}",
        f"{BOLD}Active File:{RESET}   {WHITE}{r_cur_file}{RESET} {CYAN}[{r_type}]{RESET} ({rsync_status.get('file_size', '-')})",
        f"{BOLD}Destination:{RESET}   {GRAY}{r_dest}{RESET}",
        f"{BOLD}Progress:{RESET}      {make_progress_bar(r_pct)}  {BOLD}Speed:{RESET} {GREEN}{r_speed}{RESET}  {BOLD}ETA:{RESET} {YELLOW}{r_eta}{RESET}",
        f"{BOLD}Last Synced:{RESET}   {r_last_file}  [{GREEN}{r_last_status}{RESET}] {GRAY}@ {r_last_time}{RESET}",
    ]
    if rsync_procs:
        r_proc_strs = [f"PID {p['pid']} (up {p['etime']}, CPU {p['cpu']})" for p in rsync_procs[:3]]
        rsync_lines.append(f"{BOLD}Watchdog PID:{RESET}  {GREEN}{', '.join(r_proc_strs)}{RESET}")
    else:
        rsync_lines.append(
            f"{BOLD}Watchdog PID:{RESET}  {GRAY}Not running (./rsync_mkv_to_remote.py <safe_dir> <remote> --watch){RESET}"
        )

    output_blocks.append(
        draw_box(
            "2. RSYNC REMOTE WATCHDOG & ROUTER (rsync_mkv_to_remote.py)",
            rsync_lines,
            width,
            border_color=CYAN if (rsync_locked or r_state == "TRANSFERRING") else BLUE,
        )
    )

    # 4. Recent Monthly Log Summary (docs/YYYY-MM.md)
    month_file, total_cnt, safe_cnt, skep_cnt, recent_rows = get_recent_docs_entries(max_rows=5)
    doc_lines = [
        f"{BOLD}Current Monthly File:{RESET} {WHITE}docs/{month_file}{RESET}   "
        f"({BOLD}Total:{RESET} {total_cnt}  |  {GREEN}Safe: {safe_cnt}{RESET}  |  {RED}Skeptical: {skep_cnt}{RESET})"
    ]
    if recent_rows:
        doc_lines.append(f"{DIM}{'─' * inner_w}{RESET}")
        for r_date, r_name, r_status, r_res, r_note in recent_rows:
            badge = f"{GREEN}SAFE     {RESET}" if r_status == "SAFE" else f"{RED}SKEPTICAL{RESET}"
            name_disp = truncate_plain(r_name, 34).ljust(34)
            res_disp = truncate_plain(r_res, 16).ljust(16)
            note_disp = truncate_plain(r_note, max(10, inner_w - 78))
            doc_lines.append(f"{GRAY}{r_date}{RESET}  {badge}  {WHITE}{name_disp}{RESET}  {CYAN}{res_disp}{RESET}  {DIM}{note_disp}{RESET}")
    else:
        doc_lines.append(f"{GRAY}No files logged in docs/{month_file} yet.{RESET}")

    output_blocks.append(draw_box("3. MONTHLY DOCS LOG SUMMARY (/docs)", doc_lines, width, border_color=BLUE))

    if watch_mode:
        output_blocks.append(f" {GRAY}Press {BOLD}Ctrl+C{RESET}{GRAY} to exit live dashboard.{RESET}")

    return "\n".join(output_blocks)


def main():
    parser = argparse.ArgumentParser(
        description="Display a live ASCII/Unicode dashboard for the Malware Scanner, Season Splitter, and Rsync scripts."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Render the status dashboard once and exit (instead of refreshing live)",
    )
    parser.add_argument(
        "-i", "--interval",
        type=float,
        default=1.0,
        help="Refresh interval in seconds for live mode (default: 1.0)",
    )
    args = parser.parse_args()

    if args.once or not sys.stdout.isatty():
        print(render_dashboard(watch_mode=False))
        sys.exit(0)

    # Hide cursor during live updates
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()
    try:
        while True:
            frame = render_dashboard(watch_mode=True)
            # Clear screen and move cursor to top-left
            sys.stdout.write("\033[2J\033[H" + frame + "\n")
            sys.stdout.flush()
            time.sleep(max(0.2, args.interval))
    except KeyboardInterrupt:
        pass
    finally:
        # Restore cursor
        sys.stdout.write("\033[?25h\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
