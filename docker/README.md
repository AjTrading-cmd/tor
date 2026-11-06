# Plex Media Server + Tailscale (Docker on Debian)

This directory contains the Docker configuration to build and run **Plex Media Server** alongside **Tailscale** inside a Debian Bookworm container (`debian:bookworm-slim`), configured for **full-quality direct streaming** over Tailscale without hitting the 1–2 Mbps Plex Relay limit.

## Files

- `Dockerfile`: Builds a Debian Bookworm image with the official **Plex Media Server**, **Tailscale**, `openssl`, and `xmlstarlet` packages installed.
- `entrypoint.sh`:
  - Starts `tailscaled` and authenticates with your Tailnet (`TS_AUTHKEY`).
  - Automatically provisions a Let's Encrypt TLS certificate via `tailscale cert` for your MagicDNS domain (`plex-server.<tailnet>.ts.net`), converts it to PKCS#12 (`/config/tailscale.p12`), and configures it in Plex's `Preferences.xml`.
  - Publishes `https://plex-server.<tailnet>.ts.net:32400` in Plex's `customConnections` so remote clients (browsers, Apple TV, mobile apps) can connect directly and securely over Tailscale.
  - Configures `LanNetworksBandwidth` (`100.64.0.0/10,192.168.0.0/16,172.16.0.0/12,10.0.0.0/8`) so Tailscale traffic is treated as unlimited local network bandwidth.
  - Claims the Plex server on first run if `PLEX_CLAIM` is set.
- `docker-compose.yml`: Orchestrates the container, volumes, `/dev/net/tun`, and ports.
- `.env.example`: Template environment file for configuring Tailscale auth keys, Plex claim token, and media paths on your Debian server.

---

## Quick Start on Your Debian Server

### 1. Enable MagicDNS & HTTPS Certificates in Tailscale

In the [Tailscale Admin Console (DNS settings)](https://login.tailscale.com/admin/dns):
1. Ensure **MagicDNS** is enabled.
2. Scroll down to **HTTPS Certificates** and click **Enable HTTPS**.

*(Why this matters: Plex's default `.plex.direct` certificate blocks `100.x.y.z` Tailscale CGNAT IPs via DNS rebinding protection. Using a Tailscale HTTPS certificate for `plex-server.<tailnet>.ts.net` gives Plex a valid TLS certificate that works on `app.plex.tv`, Apple TV, and mobile apps without falling back to the 1–2 Mbps Plex Relay.)*

### 2. Configure Environment Variables

Copy `.env.example` to `.env` and fill in your settings:

```bash
cd docker
cp .env.example .env
nano .env
```

Key settings in `.env`:
- `TS_AUTHKEY`: Generate a Tailscale auth key from the [Tailscale Admin Console](https://login.tailscale.com/admin/settings/keys).
- `PLEX_CLAIM`: *(First run only)* Get a 4-minute claim token at [plex.tv/claim](https://www.plex.tv/claim/). Note that `entrypoint.sh` unsets Plex's bundled `LD_LIBRARY_PATH` when calling `curl` so the claim token exchange succeeds cleanly on Debian Bookworm.
- `MEDIA_DIR`: Absolute path to your media folder on your Debian server (e.g., `/home/juju/media`). Inside the container, this is mounted at `/media`.
- `PUID` / `PGID`: Your Debian user's UID and GID (`id -u` and `id -g`, typically `1000`).

### 3. Build and Start the Container

```bash
docker compose up -d --build
```

---

## Connecting from Your Devices

### 1. Remote PC (Web Browser)

- **Recommended (Direct URL):** Bookmark and open your server's Tailscale HTTPS URL directly:
  ```text
  https://plex-server.<your-tailnet>.ts.net:32400/web
  ```
  *(For example: `https://plex-server.tail31a46b.ts.net:32400/web`)*
  Opening this URL directly loads the Plex Web app straight from your server over Tailscale with a valid HTTPS certificate and avoids Chrome/Edge "Local Network Access" prompts on `app.plex.tv`.
- **Via `https://app.plex.tv`:**
  - If `app.plex.tv` says *"unable to connect to plex-server securely"*, either click **"Or you can try to navigate directly to Plex Media Server"**, or do a hard refresh (`Ctrl+F5`) and click **Allow** if Chrome prompts to allow `app.plex.tv` to access devices on your local network.

### 2. Apple TV

1. Install **Tailscale** from the Apple TV App Store, sign in to your Tailscale account, and turn it **On**.
2. Open the **Plex** app on the Apple TV. It will automatically discover `https://plex-server.<your-tailnet>.ts.net:32400` from your Plex account and connect directly over Tailscale.
3. In the Apple TV Plex app **Settings → Video Quality**:
   - **Auto Adjust Quality:** `Off`
   - **Home Streaming:** `Maximum` (Original)
   - **Internet Streaming:** `Maximum` (Original)

### 3. Ensuring Full Video Quality (No Relay / No Unnecessary Transcoding)

If video ever looks low quality (360p/480p/720p):
1. Make sure **Tailscale is connected** on the viewing device (otherwise Plex falls back to **Plex Relay**, which caps streams at 1–2 Mbps).
2. In the player's **Settings (gear icon) → Quality**, select **Play Original Quality / Maximum** (`Convert Automatically: Off`).
3. Verify the stream in Plex **Settings → Status → Dashboard** (expand details): it should say **Local** or **Remote** (never *Indirect / Relay*) and **Direct Play** or **Direct Stream**.

---

## Useful Commands

```bash
# View container logs (Tailscale login, certificate provisioning & Plex startup)
docker compose logs -f

# Check Tailscale connection status & MagicDNS hostname inside the container
docker exec plex-tailscale tailscale --socket=/var/run/tailscale/tailscaled.sock status

# Authenticate Tailscale interactively (if you didn't provide TS_AUTHKEY)
docker exec -it plex-tailscale tailscale --socket=/var/run/tailscale/tailscaled.sock up --hostname=plex-server

# Restart the container (also refreshes the Tailscale HTTPS certificate)
docker compose restart

# Stop the container
docker compose down
```
