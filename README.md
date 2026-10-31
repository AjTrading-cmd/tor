# tor

Run this command:
./convert_mkv_audio_to_stereo.py /path/to/movies --keep-originals

## Rsync MKV Files to a Remote Server

Use `rsync_mkv_to_remote.py` to sync all `.mkv` files from a local directory on Linux Mint to a destination directory on a remote Debian server over SSH.

### Prerequisites

Make sure `rsync` and SSH are installed on both machines:
- **Linux Mint (client):** `sudo apt install rsync openssh-client`
- **Debian (remote server):** `sudo apt install rsync openssh-server`

### Usage

Pass the local folder, the remote server (`user@host`), and the remote destination folder:

```bash
./rsync_mkv_to_remote.py /path/to/local/movies user@debian-server /path/to/remote/movies
```

You can also combine the remote server and path using standard `user@host:/path` syntax:

```bash
./rsync_mkv_to_remote.py /path/to/local/movies user@192.168.1.100:/path/to/remote/movies
```

### Options

- `-n`, `--dry-run`: Preview which `.mkv` files will be transferred without making any changes.
  ```bash
  ./rsync_mkv_to_remote.py /path/to/local/movies user@debian-server /path/to/remote/movies --dry-run
  ```
- `-r`, `--recursive`: Include `.mkv` files located in subdirectories.
- `-p`, `--port <PORT>`: Specify a custom SSH port (default: `22`).
- `-i`, `--identity <KEY_FILE>`: Specify a custom SSH private key file (e.g., `~/.ssh/id_ed25519`).
- `--remove-source-files`: Delete local `.mkv` files after they have been successfully transferred to the remote server.

