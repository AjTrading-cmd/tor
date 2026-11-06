# tor

Utilities for processing and transferring `.mkv` media files.

---

## 1. Convert MKV Audio to Stereo (`convert_mkv_audio_to_stereo.py`)

Use `convert_mkv_audio_to_stereo.py` to batch-convert English audio tracks in `.mkv` files (including those in subdirectories) to 256k AAC 2.0 Stereo (using Dolby Pro Logic II matrix downmixing and LFE bass mixing) while stripping non-English audio tracks and preserving video, subtitles, and attachments without re-encoding video.

### Prerequisites

Make sure `ffmpeg` and `ffprobe` are installed:
- **Debian / Ubuntu / Linux Mint:** `sudo apt install ffmpeg`

Ensure the script is executable (if running directly):
```bash
chmod +x convert_mkv_audio_to_stereo.py
```

### Usage

Pass the folder containing your `.mkv` files:

```bash
# Convert files in-place (replaces original .mkv files)
./convert_mkv_audio_to_stereo.py /path/to/movies

# Keep original files and save converted copies as *_stereo.mkv
./convert_mkv_audio_to_stereo.py /path/to/movies --keep-originals
```

*(You can also run it via Python directly: `python3 convert_mkv_audio_to_stereo.py /path/to/movies --keep-originals`)*

### Options

- `--keep-originals`: Save converted files as `<filename>_stereo.mkv` instead of overwriting the original `.mkv` files.
- `--test [MB]`: Only process the first `500` MB of video (or specify a custom size in MB) and save the result as `<filename>_test.mkv` without modifying the original file.
- `--clev <FLOAT>`: Set the center (dialogue/voice) mix level when downmixing surround audio to stereo (default: `0.354`, ~50% of `0.707`).
- `--slev <FLOAT>`: Set the surround mix level when downmixing surround audio to stereo (default: `1.0`).
- `--lfe <FLOAT>`: Set the LFE (subwoofer bass) mix level when downmixing surround audio to stereo (default: `0.707`).
  ```bash
  # Generate a 500 MB *_test.mkv sample to test audio levels
  ./convert_mkv_audio_to_stereo.py /path/to/movies --test

  # Full conversion with custom mix levels
  ./convert_mkv_audio_to_stereo.py /path/to/movies --keep-originals --clev 0.354 --slev 1.0 --lfe 0.5
  ```

---

## 2. Rsync Video Files to a Remote Server (`rsync_mkv_to_remote.py`)

Use `rsync_mkv_to_remote.py` to sync `.mkv` and `.mp4` video files from a local directory (such as `safe_files/`) to a remote Debian server over SSH with a live progress bar, transfer summary table, **watchdog mode (`--watch`)**, and **automatic Movie vs. TV Show detection and routing**.

### Prerequisites

Make sure `rsync`, `ssh`, and `inotify-tools` (for watchdog mode) are installed:
- **Linux Mint (client):** `sudo apt install rsync openssh-client inotify-tools`
- **Debian (remote server):** `sudo apt install rsync openssh-server`

Ensure the script is executable (if running directly):
```bash
chmod +x rsync_mkv_to_remote.py
```

### Watchdog Mode on `safe_files` (Recommended)

Run `rsync_mkv_to_remote.py` with `--watch` (`-w`) pointed at your `safe_files` directory. Whenever `watch_and_scan_media.py` moves a clean `.mkv` or `.mp4` into `safe_files`, the watchdog automatically:
1. Detects whether the file is a **Movie** or **TV Show** (via `S01E02`, `1x02`, `Season 1`, `Episode 1`, dated show patterns, and `ffprobe` container metadata).
2. Transfers **Movies** to your remote Movies directory and **TV Shows** to your remote TV Shows directory (creating the remote directories automatically if needed).
3. Deletes the local file from `safe_files` once the transfer completes (pass `--keep-source-files` if you want to keep local copies).

```bash
# Automatically splits into /path/to/remote/media/movies and /path/to/remote/media/tv_shows
./rsync_mkv_to_remote.py /path/to/safe_files user@debian-server:/path/to/remote/media --watch

# Or specify explicit remote Movie and TV Show directories:
./rsync_mkv_to_remote.py /path/to/safe_files user@debian-server:/path/to/remote/movies --tv-dir /path/to/remote/tv_shows --watch
```

### One-Shot Usage

Pass the local folder (or file), the remote server (`user@host`), and the remote destination folder:

```bash
./rsync_mkv_to_remote.py /path/to/local/movies user@debian-server /path/to/remote/movies
```

You can also combine the remote server and path using standard `user@host:/path` syntax:

```bash
./rsync_mkv_to_remote.py /path/to/local/movies user@192.168.1.100:/path/to/remote/movies
```

### Options

- `-w`, `--watch`: Watch `source_dir` (e.g., `safe_files/`) continuously and sync new `.mkv`/`.mp4` files as soon as they appear (removes local file after transfer by default).
- `--tv-dir <REMOTE_PATH>`: Remote directory for TV Shows when syncing mixed Movie/TV folders.
- `--same-remote-dir`: Send both Movies and TV Shows to the exact same remote folder even in `--watch` mode.
- `--keep-source-files`: Keep local files in `safe_files` after syncing in `--watch` mode.
- `-s`, `--select`: Interactively select which `.mkv`/`.mp4` files to sync from a numbered list (shows `[Movie]` or `[TV Show]` tags).
- `-n`, `--dry-run`: Preview which `.mkv` and `.mp4` files will be transferred and where they will be routed without making any changes.
- `--no-recursive`: Only search the top-level directory (subdirectory recursion is enabled by default).
- `-p`, `--port <PORT>`: Specify a custom SSH port (default: `22`).
- `-i`, `--identity <KEY_FILE>`: Specify a custom SSH private key file (e.g., `~/.ssh/id_ed25519`).
- `--remove-source-files`: Delete local `.mkv`/`.mp4` files after they have been successfully transferred to the remote server.

---

## 3. Inspect Video Files (`inspect_video_files.py`)

Use `inspect_video_files.py` to scan all `.mkv` and `.mp4` files in a directory (including subdirectories) and pretty-print a table with each file's size, duration, resolution (e.g., `1920x1080 (1080p)`, `4K`), video codec/bit-depth/HDR/frame rate, overall bitrate, all audio tracks (language, codec, channel layout, audio bitrate), and subtitle tracks.

### Usage

```bash
./inspect_video_files.py /path/to/movies
```

### Options

- `--no-recursive`: Only inspect the top-level directory without searching subdirectories.

---

## 4. Plex Media Server + Tailscale Docker Setup (`docker/`)

The [`docker/`](docker/README.md) directory contains a Docker Compose setup that runs **Plex Media Server** and **Tailscale** together inside a single Debian Bookworm container with automatic Tailscale HTTPS certificate provisioning for full-quality direct streaming (no 1–2 Mbps Plex Relay bottleneck) across remote networks, PCs, and Apple TVs.

### Quick Start

```bash
cd docker
cp .env.example .env
# Edit .env with your TS_AUTHKEY, PLEX_CLAIM, and MEDIA_DIR
docker compose up -d --build
```

### Direct Access over Tailscale

Once Tailscale and **HTTPS Certificates** (at [login.tailscale.com/admin/dns](https://login.tailscale.com/admin/dns)) are enabled on your tailnet:

- **PC / Browser (recommended bookmark):** `https://plex-server.<your-tailnet>.ts.net:32400/web`
- **Apple TV / Mobile Apps:** Connect the Tailscale app on the device and open the native Plex app — it automatically discovers and streams directly via `https://plex-server.<your-tailnet>.ts.net:32400`.

See [`docker/README.md`](docker/README.md) for full setup, Apple TV configuration, quality/Direct Play settings, and troubleshooting.

---

## 5. Watch & Scan Incoming Media Files (`watch_and_scan_media.py`)

Use `watch_and_scan_media.py` to automatically scan finished `.mkv` and `.mp4` downloads (directly via **qBittorrent's "Run external program on torrent finished"**, a real-time folder watcher, or a cronjob) using **ClamAV** (`clamdscan` or `clamscan`) and container integrity checks (`ffprobe`).

- **Passes scan:** Moved into `safe_files/` (or, if the file is a **combined full-season video**, automatically split into individual episode files like `Show.S01E01.mkv`, `Show.S01E02.mkv` inside `safe_files/` via [`split_season_video.py`](split_season_video.py) and the combined file is removed).
- **Fails scan:** Moved into `skeptical_files/`
- **Monthly Documentation (`docs/`):** Appends file type, resolution, audio format, subtitle format, and safety notes to `docs/YYYY-MM.md` (creating a new `.md` file on the 1st of every month), updates the landing page [`docs/index.md`](docs/index.md) for GitHub / GitHub Pages, and automatically commits & pushes the updated `docs/` folder to GitHub.

### Prerequisites

Install `ffmpeg`, `clamav`, and `inotify-tools`:
```bash
sudo apt install ffmpeg clamav clamav-daemon inotify-tools
```

### Usage with qBittorrent (Recommended)

1. Open **qBittorrent** $\rightarrow$ **Tools** $\rightarrow$ **Options...** (`Alt + O`) $\rightarrow$ **Downloads**.
2. Scroll to the bottom and check **"Run external program on torrent finished"**.
3. Enter the following command (using `%F`, which qBittorrent replaces with the finished torrent's file or folder path):
   ```bash
   /home/juju/ghe/tor/watch_and_scan_media.py "%F" --once --safe-dir "/path/to/safe_files" --skeptical-dir "/path/to/skeptical_files"
   ```
   *(If you omit `--safe-dir` and `--skeptical-dir`, they will automatically be created as `safe_files/` and `skeptical_files/` next to your download directory.)*

### Enabling GitHub Pages for `/docs`
1. On your GitHub repository page, go to **Settings** $\rightarrow$ **Pages**.
2. Under **Build and deployment**:
   - **Source:** `Deploy from a branch`
   - **Branch:** `main` and folder `/docs`
3. Click **Save**. GitHub Pages will render [`docs/index.md`](docs/index.md) and each monthly [`docs/YYYY-MM.md`](docs/2026-11.md) table automatically whenever a torrent finishes.

### Other Modes

#### Real-time Folder Watcher
```bash
./watch_and_scan_media.py /path/to/incoming \
  --safe-dir /path/to/safe_files \
  --skeptical-dir /path/to/skeptical_files
```

#### Cronjob Mode (`--once`)
```cron
*/5 * * * * /usr/bin/flock -n /tmp/media_scan.lock /home/juju/ghe/tor/watch_and_scan_media.py /path/to/incoming --once >> /var/log/media_scan.log 2>&1
```

---

## 6. Split Full-Season Video Files into Episodes (`split_season_video.py`)

Use `split_season_video.py` to detect and losslessly split combined full-season `.mkv` and `.mp4` files (e.g., `Show.Name.S01E01-E10.1080p.mkv` or `Show.Name.Season.1.Complete.mkv`) into individual episode files (`Show.Name.S01E01.1080p.mkv`, `Show.Name.S01E02.1080p.mkv`, etc.) without re-encoding (`ffmpeg -c copy`).

*(Note: `watch_and_scan_media.py` automatically invokes this whenever a clean full-season video file passes the malware scan, outputting the split episodes directly into `safe_files/`.)*

### How Episode Boundaries Are Determined
1. **Embedded Chapters (`ffprobe -show_chapters`):** Uses exact chapter timestamps if the file has episode-level chapters, or snaps to the nearest chapter boundary when episodes have multiple internal chapters.
2. **Black-Frame & Silence Detection (`ffmpeg blackdetect` / `silencedetect`):** Scans a transition window around each expected episode break to find the exact black screen or audio silence between episodes.
3. **Equal-Duration Fallback:** Splits evenly across the episode count if no chapters or black frames are present.

### Standalone Usage

```bash
# Auto-detect and split a combined season file (or all combined season files in a folder)
./split_season_video.py /path/to/Show.Name.S01E01-E08.mkv --output-dir /path/to/safe_files

# Explicitly specify episode count and season number, or preview with --dry-run
./split_season_video.py /path/to/Show.Season.1.mkv --episodes 10 --season 1 --dry-run
```

---

## 7. Live Pipeline Status Dashboard (`pipeline_status_ui.py`)

Use `pipeline_status_ui.py` to launch a live, colorized ASCII/Unicode terminal dashboard showing the current status, mutex locks, queue depth, active files, transfer progress bars, and recent `/docs` log entries for `watch_and_scan_media.py`, `split_season_video.py`, and `rsync_mkv_to_remote.py`.

### Usage

```bash
# Live auto-refreshing dashboard (updates every 1s)
./pipeline_status_ui.py

# Print a single status snapshot and exit
./pipeline_status_ui.py --once
```

---

## 8. NordVPN (Gluetun) + qBittorrent + Jackett + FlareSolverr Docker Stack (`docker/docker-compose.vpn-media.yml`)

Use [`docker/docker-compose.vpn-media.yml`](docker/docker-compose.vpn-media.yml) to run **NordVPN (`gluetun`)**, **qBittorrent**, **Jackett**, and **FlareSolverr** together inside a single isolated VPN network stack (`network_mode: "service:vpn"`):

- **Host PC stays off the VPN:** Your Linux Mint host keeps full-speed local LAN access (`rsync` to `192.168.52.18`, SSH, GitHub, etc. are never blocked by NordVPN's host firewall).
- **100% Leak-Proof Kill Switch:** `qbittorrent`, `jackett`, and `flaresolverr` share the `vpn` container's network interface and cannot leak traffic if the VPN drops.
- **Zero DNS / Firewall Issues:** Inside the shared network stack, qBittorrent reaches Jackett at `http://127.0.0.1:9117` and Jackett reaches FlareSolverr at `http://127.0.0.1:8191`, while your host accesses the Web UIs at `http://localhost:8080` (qBittorrent) and `http://localhost:9117` (Jackett).

### Quick Start

```bash
cd /home/juju/ghe/tor/docker
cp .env.vpn.example .env.vpn

# 1. Extract your NordLynx WireGuard private key while connected to NordVPN on host:
sudo wg show nordlynx private-key
# Paste that key into WIREGUARD_PRIVATE_KEY= in .env.vpn

# 2. Disconnect host NordVPN (since the Docker container will now handle the VPN):
nordvpn disconnect

# 3. Stop old standalone jackett/flaresolverr containers and launch the unified VPN stack:
docker rm -f jackett flaresolverr 2>/dev/null || true
docker compose -f docker-compose.vpn-media.yml --env-file .env.vpn up -d
```

### Important: Update FlareSolverr URL in Jackett
Because `jackett` and `flaresolverr` now share the same `vpn` network stack (`localhost`), in the Jackett Web UI (`http://localhost:9117`) set **FlareSolverr API URL** to `http://127.0.0.1:8191` (or `http://localhost:8191`).






