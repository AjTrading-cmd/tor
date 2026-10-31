# tor

Utilities for processing and transferring `.mkv` media files.

---

## 1. Convert MKV Audio to Stereo (`convert_mkv_audio_to_stereo.py`)

Use `convert_mkv_audio_to_stereo.py` to batch-convert English audio tracks in `.mkv` files to 256k AAC 2.0 Stereo (using Dolby Pro Logic II matrix downmixing and LFE bass mixing) while stripping non-English audio tracks and preserving video, subtitles, and attachments without re-encoding video.

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
- `--lfe <FLOAT>`: Set the LFE (subwoofer bass) mix level when downmixing surround audio to stereo (default: `0.707`).
  ```bash
  ./convert_mkv_audio_to_stereo.py /path/to/movies --keep-originals --lfe 0.5
  ```

---

## 2. Rsync MKV Files to a Remote Server (`rsync_mkv_to_remote.py`)

Use `rsync_mkv_to_remote.py` to sync all `.mkv` files from a local directory on Linux Mint to a destination directory on a remote Debian server over SSH with a live progress bar and transfer summary table.

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

- `-n`, `--dry-run`: Preview which `.mkv` files will be transferred without making any changes.
  ```bash
  ./rsync_mkv_to_remote.py /path/to/local/movies user@debian-server /path/to/remote/movies --dry-run
  ```
- `-r`, `--recursive`: Include `.mkv` files located in subdirectories (preserving relative folder structure on the remote server).
- `-p`, `--port <PORT>`: Specify a custom SSH port (default: `22`).
- `-i`, `--identity <KEY_FILE>`: Specify a custom SSH private key file (e.g., `~/.ssh/id_ed25519`).
- `--remove-source-files`: Delete local `.mkv` files after they have been successfully transferred to the remote server.

