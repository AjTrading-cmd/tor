# Plex Media Server + Tailscale (Docker on Debian)

This directory contains the Docker configuration to build and run **Plex Media Server** alongside **Tailscale** inside a Debian Bookworm container (`debian:bookworm-slim`).

## Files

- `Dockerfile`: Builds a Debian Bookworm image with the official **Plex Media Server** and **Tailscale** packages installed.
- `entrypoint.sh`: Starts `tailscaled`, authenticates with your Tailnet (`TS_AUTHKEY`), registers the container's Tailscale IP in Plex's `customConnections`, optionally claims the Plex server (`PLEX_CLAIM`), and runs Plex Media Server.
- `docker-compose.yml`: Orchestrates the container, volumes, devices (`/dev/net/tun`), and ports.
- `.env.example`: Template environment file for configuring Tailscale auth keys, Plex claim token, and media paths on your Debian server.

---

## Quick Start on Your Debian Server

### 1. Configure Environment Variables

Copy `.env.example` to `.env` and fill in your settings:

```bash
cd docker
cp .env.example .env
nano .env
```

Key settings in `.env`:
- `TS_AUTHKEY`: Generate a Tailscale auth key from the [Tailscale Admin Console](https://login.tailscale.com/admin/settings/keys).
- `PLEX_CLAIM`: (First run only) Get a 4-minute claim token at [plex.tv/claim](https://www.plex.tv/claim/).
- `MEDIA_DIR`: Absolute path to your media folder on your Debian server (e.g., `/srv/media` or `/home/user/movies`).
- `PUID` / `PGID`: Your Debian user's UID and GID (`id -u` and `id -g`, typically `1000`).

### 2. Build and Start the Container

```bash
docker compose up -d --build
```

### 3. Access Plex Media Server

- **Via Tailscale:**
  Check the container's Tailscale IP or hostname:
  ```bash
  docker exec plex-tailscale tailscale ip -4
  ```
  Then open:
  ```text
  http://<TAILSCALE_IP>:32400/web
  ```
  *(Or `http://plex-server:32400/web` if MagicDNS is enabled on your Tailnet.)*

- **Via Local LAN:**
  ```text
  http://<DEBIAN_SERVER_LAN_IP>:32400/web
  ```

---

## Useful Commands

```bash
# View container logs (Tailscale login status & Plex startup)
docker compose logs -f

# Check Tailscale connection status inside the container
docker exec plex-tailscale tailscale status

# Authenticate Tailscale interactively (if you didn't provide TS_AUTHKEY)
docker exec -it plex-tailscale tailscale up --hostname=plex-server

# Stop the container
docker compose down
```
