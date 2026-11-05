FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PLEX_MEDIA_SERVER_APPLICATION_SUPPORT_DIR="/config/Library/Application Support" \
    PLEX_MEDIA_SERVER_HOME="/usr/lib/plexmediaserver" \
    PLEX_MEDIA_SERVER_MAX_PLUGIN_PROCS="6" \
    PLEX_MEDIA_SERVER_TMPDIR="/transcode" \
    PLEX_MEDIA_SERVER_INFO_VENDOR="Docker" \
    PLEX_MEDIA_SERVER_INFO_DEVICE="Docker Container (Debian + Tailscale)" \
    LD_LIBRARY_PATH="/usr/lib/plexmediaserver/lib" \
    LC_ALL="C.UTF-8" \
    LANG="C.UTF-8" \
    TZ="UTC" \
    TS_STATE_DIR="/var/lib/tailscale" \
    TS_HOSTNAME="plex-server"

# Prevent service autostart during package installation in Docker build
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d && \
    chmod +x /usr/sbin/policy-rc.d && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gnupg \
        iproute2 \
        iptables \
        locales \
        procps \
        gosu \
        tzdata \
        udev \
        uuid-runtime \
        xmlstarlet && \
    # Add official Tailscale Debian Bookworm repository
    curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.noarmor.gpg \
        -o /usr/share/keyrings/tailscale-archive-keyring.gpg && \
    curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.tailscale-keyring.list \
        -o /etc/apt/sources.list.d/tailscale.list && \
    # Add official Plex Media Server Debian repository
    curl -fsSL https://downloads.plex.tv/plex-keys/PlexSign.key \
        | gpg --dearmor -o /usr/share/keyrings/plex-archive-keyring.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/plex-archive-keyring.gpg] https://downloads.plex.tv/repo/deb public main" \
        > /etc/apt/sources.list.d/plexmediaserver.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        tailscale \
        plexmediaserver && \
    rm -f /usr/sbin/policy-rc.d && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# Create required runtime directories
RUN mkdir -p \
    /config \
    /transcode \
    /media/movies \
    /media/tv \
    /media/music \
    /var/lib/tailscale \
    /var/run/tailscale && \
    chown -R plex:plex /config /transcode /media

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Expose Plex ports
# 32400/tcp: Plex Media Server web UI & streaming
# 1900/udp:  Plex DLNA Server
# 5353/udp:  Bonjour/Avahi network discovery
# 8324/tcp:  Plex Companion (Roku/PHT)
# 32410, 32412, 32413, 32414/udp: GDM network discovery
# 32469/tcp: Plex DLNA Server
EXPOSE 32400/tcp 8324/tcp 32469/tcp 1900/udp 5353/udp 32410/udp 32412/udp 32413/udp 32414/udp

VOLUME ["/config", "/transcode", "/media", "/var/lib/tailscale"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -fsS http://127.0.0.1:32400/identity >/dev/null && tailscale status >/dev/null || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
