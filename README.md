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

Use `rsync_mkv_to_remote.py` to sync all `.mkv` and `.mp4` video files (including those inside subdirectories, copying only the video files directly into the remote folder without recreating their subdirectories or copying non-video files) from a local directory on Linux Mint to a destination directory on a remote Debian server over SSH with a live progress bar and transfer summary table.

### Prerequisites

Make sure `rsync` and SSH are installed on both machines:
- **Linux Mint (client):** `sudo apt install rsync openssh-client`
- **Debian (remote server):** `sudo apt install rsync openssh-server`

Ensure the script is executable (if running directly):
```bash
chmod +x rsync_mkv_to_remote.py
```

### Usage

Pass the local folder, the remote server (`user@host`), and the remote destination folder:

```bash
./rsync_mkv_to_remote.py /path/to/local/movies user@debian-server /path/to/remote/movies
```

You can also combine the remote server and path using standard `user@host:/path` syntax:

```bash
./rsync_mkv_to_remote.py /path/to/local/movies user@192.168.1.100:/path/to/remote/movies
```

*(You can also run it via Python directly: `python3 rsync_mkv_to_remote.py /path/to/local/movies user@debian-server /path/to/remote/movies`)*

### Options

- `-n`, `--dry-run`: Preview which `.mkv` and `.mp4` files will be transferred without making any changes.
  ```bash
  ./rsync_mkv_to_remote.py /path/to/local/movies user@debian-server /path/to/remote/movies --dry-run
  ```
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

- **Passes scan:** Moved into `safe_files/`
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



