#!/usr/bin/env bash
set -euo pipefail

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*"
}

# 1. Configure Timezone
if [ -n "${TZ:-}" ] && [ -f "/usr/share/zoneinfo/${TZ}" ]; then
    ln -snf "/usr/share/zoneinfo/${TZ}" /etc/localtime
    echo "${TZ}" > /etc/timezone
fi

# 2. Configure PUID / PGID for the plex user
PUID="${PUID:-}"
PGID="${PGID:-}"

if [ -n "${PGID}" ]; then
    CURRENT_GID="$(id -g plex)"
    if [ "${PGID}" != "${CURRENT_GID}" ]; then
        log "Updating plex group GID to ${PGID}..."
        groupmod -o -g "${PGID}" plex
    fi
fi

if [ -n "${PUID}" ]; then
    CURRENT_UID="$(id -u plex)"
    if [ "${PUID}" != "${CURRENT_UID}" ]; then
        log "Updating plex user UID to ${PUID}..."
        usermod -o -u "${PUID}" plex
    fi
fi

# Grant access to /dev/dri for hardware transcoding if present
if [ -d /dev/dri ]; then
    for dev in /dev/dri/renderD* /dev/dri/card*; do
        if [ -e "${dev}" ]; then
            DEV_GID="$(stat -c '%g' "${dev}")"
            if ! getent group "${DEV_GID}" >/dev/null; then
                groupadd -g "${DEV_GID}" "videodri_${DEV_GID}" || true
            fi
            GRP_NAME="$(getent group "${DEV_GID}" | cut -d: -f1)"
            usermod -aG "${GRP_NAME}" plex || true
        fi
    done
fi

# 3. Prepare directories & permissions
mkdir -p \
    "${PLEX_MEDIA_SERVER_APPLICATION_SUPPORT_DIR}/Plex Media Server" \
    "${PLEX_MEDIA_SERVER_TMPDIR}" \
    "${TS_STATE_DIR:-/var/lib/tailscale}" \
    /var/run/tailscale

chown -R plex:plex /config "${PLEX_MEDIA_SERVER_TMPDIR}"

# Remove stale Plex PID file if present from an unclean stop
PID_FILE="${PLEX_MEDIA_SERVER_APPLICATION_SUPPORT_DIR}/Plex Media Server/plexmediaserver.pid"
if [ -f "${PID_FILE}" ]; then
    log "Removing stale Plex PID file: ${PID_FILE}"
    rm -f "${PID_FILE}"
fi

# 4. Start Tailscale daemon (tailscaled)
log "Starting tailscaled..."
TS_SOCKET="/var/run/tailscale/tailscaled.sock"
TS_STATE="${TS_STATE_DIR:-/var/lib/tailscale}/tailscaled.state"
TS_DAEMON_ARGS=("--state=${TS_STATE}" "--socket=${TS_SOCKET}")

# Ensure /dev/net/tun exists when running in kernel networking mode
if [ "${TS_USERSPACE:-false}" = "true" ]; then
    log "Using Tailscale userspace networking mode."
    TS_DAEMON_ARGS+=("--tun=userspace-networking")
else
    if [ ! -e /dev/net/tun ]; then
        mkdir -p /dev/net
        if ! mknod /dev/net/tun c 10 200 2>/dev/null; then
            log "Warning: /dev/net/tun unavailable; falling back to userspace networking."
            TS_DAEMON_ARGS+=("--tun=userspace-networking")
        fi
    fi
fi

tailscaled "${TS_DAEMON_ARGS[@]}" &
TAILSCALED_PID=$!

# Wait for tailscaled socket to become ready
for _ in $(seq 1 30); do
    if [ -S "${TS_SOCKET}" ]; then
        break
    fi
    sleep 0.5
done

# Build tailscale up arguments
TS_UP_ARGS=("--hostname=${TS_HOSTNAME:-plex-server}")
if [ -n "${TS_EXTRA_ARGS:-}" ]; then
    # shellcheck disable=SC2206
    EXTRA_ARR=(${TS_EXTRA_ARGS})
    TS_UP_ARGS+=("${EXTRA_ARR[@]}")
fi

log "Bringing up Tailscale interface (hostname: ${TS_HOSTNAME:-plex-server})..."
if tailscale --socket="${TS_SOCKET}" status >/dev/null 2>&1; then
    log "Tailscale node already authenticated via persisted state."
    tailscale --socket="${TS_SOCKET}" up "${TS_UP_ARGS[@]}" || log "Warning: 'tailscale up' returned non-zero."
elif [ -n "${TS_AUTHKEY:-}" ]; then
    TS_UP_ARGS+=("--authkey=${TS_AUTHKEY}")
    tailscale --socket="${TS_SOCKET}" up "${TS_UP_ARGS[@]}" || log "Warning: 'tailscale up' returned non-zero. Run 'docker exec <container> tailscale up' to authenticate interactively if needed."
else
    log "No TS_AUTHKEY provided and node is not yet authenticated."
    log "Starting non-blocking 'tailscale up' to generate login URL (check container logs)..."
    tailscale --socket="${TS_SOCKET}" up "${TS_UP_ARGS[@]}" &
fi

# Retrieve Tailscale IP if connected
TS_IP="$(tailscale --socket="${TS_SOCKET}" ip -4 2>/dev/null | head -n1 || true)"
if [ -n "${TS_IP}" ]; then
    log "Tailscale connected! Tailscale IPv4: ${TS_IP}"
    log "Plex Web UI via Tailscale: http://${TS_IP}:32400/web"
fi

# Optional: Enable Tailscale Serve (HTTPS proxy to Plex port 32400)
if [ "${TS_SERVE:-false}" = "true" ] && [ -n "${TS_IP}" ]; then
    log "Configuring Tailscale Serve for port 32400..."
    tailscale --socket="${TS_SOCKET}" serve --bg 32400 || log "Warning: Failed to enable tailscale serve."
fi

# 5. Configure Plex Preferences.xml & Claim Server (if PLEX_CLAIM is provided)
PREFS_FILE="${PLEX_MEDIA_SERVER_APPLICATION_SUPPORT_DIR}/Plex Media Server/Preferences.xml"

get_pref() {
    local key="$1"
    if [ -f "${PREFS_FILE}" ]; then
        xmlstarlet sel -t -v "/Preferences/@${key}" "${PREFS_FILE}" 2>/dev/null || true
    fi
}

set_pref() {
    local key="$1"
    local val="$2"
    if [ "$(xmlstarlet sel -t -v "count(/Preferences/@${key})" "${PREFS_FILE}" 2>/dev/null || echo 0)" -gt 0 ]; then
        xmlstarlet ed --inplace -u "/Preferences/@${key}" -v "${val}" "${PREFS_FILE}"
    else
        xmlstarlet ed --inplace -i "/Preferences" -t attr -n "${key}" -v "${val}" "${PREFS_FILE}"
    fi
}

if [ ! -f "${PREFS_FILE}" ]; then
    log "Creating initial Plex Preferences.xml..."
    cat > "${PREFS_FILE}" <<EOF
<?xml version="1.0" encoding="utf-8"?>
<Preferences/>
EOF
    chown plex:plex "${PREFS_FILE}"
fi

MACHINE_ID="$(get_pref "MachineIdentifier")"
if [ -z "${MACHINE_ID}" ]; then
    MACHINE_ID="$(uuidgen)"
    set_pref "MachineIdentifier" "${MACHINE_ID}"
fi

PROCESSED_ID="$(get_pref "ProcessedMachineIdentifier")"
if [ -z "${PROCESSED_ID}" ]; then
    PROCESSED_ID="$(printf '%s- Plex Media Server' "${MACHINE_ID}" | sha1sum | cut -d' ' -f1)"
    set_pref "ProcessedMachineIdentifier" "${PROCESSED_ID}"
fi

ONLINE_TOKEN="$(get_pref "PlexOnlineToken")"
if [ -n "${PLEX_CLAIM:-}" ] && [ -z "${ONLINE_TOKEN}" ]; then
    log "Attempting to claim Plex Media Server using PLEX_CLAIM token..."
    CLAIM_RESPONSE="$(env -u LD_LIBRARY_PATH curl -fsSL -X POST \
        -H "X-Plex-Client-Identifier: ${PROCESSED_ID}" \
        -H "X-Plex-Product: Plex Media Server" \
        -H "X-Plex-Version: 1.1" \
        -H "X-Plex-Provides: server" \
        -H "X-Plex-Platform: Linux" \
        -H "X-Plex-Platform-Version: Debian" \
        -H "X-Plex-Device-Name: ${TS_HOSTNAME:-plex-server}" \
        -H "X-Plex-Device: Linux" \
        "https://plex.tv/api/claim/exchange?token=${PLEX_CLAIM}" || true)"

    if [ -n "${CLAIM_RESPONSE}" ]; then
        CLAIMED_TOKEN="$(printf '%s' "${CLAIM_RESPONSE}" | xmlstarlet sel -t -v "/authentication-token" 2>/dev/null || true)"
        if [ -n "${CLAIMED_TOKEN}" ]; then
            set_pref "PlexOnlineToken" "${CLAIMED_TOKEN}"
            log "Plex Media Server claimed successfully!"
        else
            log "Warning: Could not extract token from Plex claim response. Check if PLEX_CLAIM is valid/unexpired."
        fi
    else
        log "Warning: Failed to contact plex.tv claim endpoint."
    fi
fi

# Register Tailscale MagicDNS hostname & HTTPS certificate in Plex Preferences.xml
TS_DNS_NAME="$(tailscale --socket="${TS_SOCKET}" status --self --json 2>/dev/null | grep -m1 '"DNSName"' | sed -E 's/.*"DNSName": *"([^"]+)\.?".*/\1/' | sed 's/\.$//' || true)"
TS_CERT_READY="false"

if [ -n "${TS_DNS_NAME}" ] && [ "${ADVERTISE_TAILSCALE_IP:-true}" = "true" ]; then
    TS_CRT="/config/tailscale.crt"
    TS_KEY="/config/tailscale.key"
    TS_P12="/config/tailscale.p12"
    TS_P12_PASS="plextailscale"

    if tailscale --socket="${TS_SOCKET}" status --json 2>/dev/null | grep -q '"CertDomains": *\['; then
        log "Provisioning Tailscale HTTPS certificate for ${TS_DNS_NAME}..."
        if tailscale --socket="${TS_SOCKET}" cert --cert-file="${TS_CRT}" --key-file="${TS_KEY}" "${TS_DNS_NAME}" >/dev/null 2>&1; then
            if env -u LD_LIBRARY_PATH openssl pkcs12 -export \
                -out "${TS_P12}" \
                -inkey "${TS_KEY}" \
                -in "${TS_CRT}" \
                -name "${TS_DNS_NAME}" \
                -passout "pass:${TS_P12_PASS}" >/dev/null 2>&1; then
                chown plex:plex "${TS_P12}" "${TS_CRT}" "${TS_KEY}"
                chmod 600 "${TS_P12}" "${TS_KEY}"
                set_pref "customCertificatePath" "${TS_P12}"
                set_pref "customCertificateKey" "${TS_P12_PASS}"
                set_pref "customCertificateDomain" "${TS_DNS_NAME}"
                TS_CERT_READY="true"
                log "Tailscale HTTPS certificate configured for Plex: https://${TS_DNS_NAME}:32400"
            else
                log "Warning: Failed to convert Tailscale certificate to PKCS#12 format."
            fi
        else
            log "Warning: 'tailscale cert' failed for ${TS_DNS_NAME}."
        fi
    else
        log "Note: Tailscale HTTPS Certificates are not yet enabled on your tailnet."
        log "Enable 'HTTPS Certificates' at https://login.tailscale.com/admin/dns — container will auto-detect and configure HTTPS."
    fi

    if [ "${TS_CERT_READY}" = "true" ]; then
        TS_URL="https://${TS_DNS_NAME}:32400,http://${TS_DNS_NAME}:32400"
    else
        TS_URL="http://${TS_DNS_NAME}:32400"
    fi

    EXISTING_CUSTOM="$(get_pref "customConnections")"
    # Strip any previously added 100.x.x.x CGNAT URLs or stale TS_DNS_NAME entries before prepending TS_URL
    CLEANED_CUSTOM="$(printf '%s' "${EXISTING_CUSTOM}" | sed -E "s#https?://(100\.[0-9]+\.[0-9]+\.[0-9]+|${TS_DNS_NAME}):[0-9]+,?# #g; s/^[, ]+//; s/[, ]+$//; s/ +/,/g")"
    if [ -z "${CLEANED_CUSTOM}" ]; then
        set_pref "customConnections" "${TS_URL}"
    else
        set_pref "customConnections" "${TS_URL},${CLEANED_CUSTOM}"
    fi
fi

# Ensure secureConnections defaults to Preferred (1) so both HTTPS and HTTP fallback work
EXISTING_SECURE="$(get_pref "secureConnections")"
if [ -z "${EXISTING_SECURE}" ]; then
    set_pref "secureConnections" "1"
fi

# Treat Tailscale CGNAT (100.64.0.0/10) and standard RFC1918 ranges as LAN in Plex
EXISTING_LAN="$(get_pref "LanNetworksBandwidth")"
if [ -z "${EXISTING_LAN}" ]; then
    set_pref "LanNetworksBandwidth" "100.64.0.0/10,192.168.0.0/16,172.16.0.0/12,10.0.0.0/8"
fi

chown plex:plex "${PREFS_FILE}"

# If Tailscale HTTPS wasn't enabled at boot, watch in background and auto-restart once enabled in Tailscale Admin Console
MAIN_PID=$$
if [ -n "${TS_DNS_NAME}" ] && [ "${ADVERTISE_TAILSCALE_IP:-true}" = "true" ] && [ "${TS_CERT_READY}" != "true" ]; then
    (
        while true; do
            sleep 10
            if tailscale --socket="${TS_SOCKET}" status --json 2>/dev/null | grep -q '"CertDomains": *\['; then
                log "Detected Tailscale HTTPS Certificates enabled! Restarting container to provision TLS certificate..."
                kill -TERM "${MAIN_PID}" 2>/dev/null || true
                break
            fi
        done
    ) &
fi

# 6. Trap signals for clean shutdown
PLEX_PID=""
cleanup() {
    log "Shutting down Plex Media Server and Tailscale..."
    if [ -n "${PLEX_PID}" ] && kill -0 "${PLEX_PID}" 2>/dev/null; then
        kill -TERM "${PLEX_PID}" 2>/dev/null || true
        wait "${PLEX_PID}" 2>/dev/null || true
    fi
    if [ -n "${TAILSCALED_PID:-}" ] && kill -0 "${TAILSCALED_PID}" 2>/dev/null; then
        kill -TERM "${TAILSCALED_PID}" 2>/dev/null || true
        wait "${TAILSCALED_PID}" 2>/dev/null || true
    fi
    exit 0
}
trap cleanup SIGTERM SIGINT SIGQUIT

# 7. Launch Plex Media Server
log "Starting Plex Media Server..."
gosu plex "/usr/lib/plexmediaserver/Plex Media Server" &
PLEX_PID=$!

wait "${PLEX_PID}"
