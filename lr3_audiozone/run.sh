#!/usr/bin/env bash
# LR3 AudioZone — Spotify Connect -> Icecast mount -> SlimProto push do ELKO LARA.
# librespot (Spotify) krmí Liquidsoap -> Icecast /default; SlimProto controller najde
# LARA rádia a když Spotify hraje, pushne je na /default (aktivuje "audio zónu" v LAŘE).
# Tento skript je PID 1 kontejneru addonu.
set -uo pipefail

OPTIONS=/data/options.json
TPL_DIR=/etc/lr3
MOUNT=default          # jeden sdílený audio-zone mount pro všechna LARA rádia

log() { echo "[LR3AZ] $*"; }

PORT=$(jq -r '.port // 8121' "$OPTIONS")
SRCPASS=$(jq -r '.source_password // "changeme"' "$OPTIONS")
BITRATE=$(jq -r '.bitrate // 192' "$OPTIONS")
SPOTIFY_BITRATE=$(jq -r '.spotify_bitrate // 320' "$OPTIONS")
ZONE_NAME=$(jq -r '.zone_name // "Audio zóna"' "$OPTIONS")
CMODE=$(jq -r '.control_mode // "slimproto"' "$OPTIONS")
IDLE_TIMEOUT=$(jq -r '.idle_timeout // 20' "$OPTIONS")
CLI_PORT=$(jq -r '.cli_port // 9595' "$OPTIONS")

# Zjisti LAN IP hostitele (host_network: true → kontejner ji sdílí).
HA_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
[ -z "$HA_IP" ] && HA_IP="<HA_IP>"
ICE_HOSTNAME="$HA_IP"
[ "$ICE_HOSTNAME" = "<HA_IP>" ] && ICE_HOSTNAME="localhost"

log "Startuji LR3 AudioZone (port=${PORT}, bitrate=${BITRATE}k, spotify=${SPOTIFY_BITRATE}k, mode=${CMODE})"
log "Audio zóna: Spotify hraje → LARA se přepne; po ${IDLE_TIMEOUT}s nečinnosti se LARA vypne"

# --- D-Bus + Avahi (librespot z raspotify používá avahi zeroconf backend) ---
log "Spouštím D-Bus + Avahi (pro Spotify Connect discovery)..."
mkdir -p /run/dbus /run/avahi-daemon
rm -f /run/dbus/pid
dbus-uuidgen --ensure 2>/dev/null || true
if dbus-daemon --system --fork; then log "D-Bus běží"; else log "VAROVÁNÍ: D-Bus se nespustil"; fi
sleep 1
if avahi-daemon --no-chroot --no-drop-root --no-rlimits --daemonize; then
  log "Avahi běží"
else
  log "VAROVÁNÍ: Avahi se nespustil — Spotify discovery nemusí fungovat"
fi

# --- Vygeneruj Icecast konfiguraci ze šablony ---
sed -e "s|%%PORT%%|${PORT}|g" \
    -e "s|%%SOURCE_PASSWORD%%|${SRCPASS}|g" \
    -e "s|%%HOSTNAME%%|${ICE_HOSTNAME}|g" \
    "${TPL_DIR}/icecast.xml.tpl" > /etc/icecast.xml

mkdir -p /var/log/icecast2
chown -R icecast2:icecast /var/log/icecast2 2>/dev/null || true

# --- Spusť Icecast ---
log "Spouštím Icecast..."
icecast2 -c /etc/icecast.xml &
ICECAST_PID=$!

for _ in $(seq 1 30); do
  nc -z localhost "${PORT}" 2>/dev/null && break
  sleep 0.5
done
if nc -z localhost "${PORT}" 2>/dev/null; then
  log "Icecast běží na :${PORT}"
else
  log "VAROVÁNÍ: Icecast zatím není dostupný na :${PORT} — pokračuji"
fi

# --- Spotify play/stop události → stavové soubory per mount (pro controller) ---
mkdir -p /etc/lr3
cat > /etc/lr3/spotify_event.sh <<'EOF'
#!/usr/bin/env sh
printf '%s' "${PLAYER_EVENT:-}" > "/tmp/spotify_state_${LR3_MOUNT:-unknown}"
EOF
chmod +x /etc/lr3/spotify_event.sh

# --- Audio-zone stream: Spotify zařízení "<ZONE_NAME>" -> mount /default ---
: > "/tmp/librespot_${MOUNT}.log"
mkdir -p "/data/librespot_${MOUNT}"
LIQ="/tmp/zone_${MOUNT}.liq"
sed -e "s|%%PORT%%|${PORT}|g" \
    -e "s|%%SOURCE_PASSWORD%%|${SRCPASS}|g" \
    -e "s|%%BITRATE%%|${BITRATE}|g" \
    -e "s|%%SPOTIFY_BITRATE%%|${SPOTIFY_BITRATE}|g" \
    -e "s|%%MOUNT%%|${MOUNT}|g" \
    -e "s|%%ZONE_NAME%%|${ZONE_NAME}|g" \
    "${TPL_DIR}/radio.liq.tpl" > "${LIQ}"
liquidsoap "${LIQ}" &
LIQ_PID=$!
log "Spotify zařízení '${ZONE_NAME}'  ->  http://${HA_IP}:${PORT}/${MOUNT}"

# Vypisuj librespot stderr do logu addonu (kvůli diagnostice).
tail -qF /tmp/librespot_*.log 2>/dev/null | sed -u 's/^/[librespot] /' &

echo "=================================================================="
echo "  LR3 AudioZone"
echo "------------------------------------------------------------------"
echo "  Spotify zařízení:  ${ZONE_NAME}   (vyber v appce, Premium, stejná síť)"
echo "  Icecast mount:     http://${HA_IP}:${PORT}/${MOUNT}"
echo "  Režim ovládání:    ${CMODE}   (SlimProto :3483 + LMS CLI :${CLI_PORT})"
echo "  V LAŘE nastav:     Audio zone function = ZAP, slim server IP = ${HA_IP}, CLI port = ${CLI_PORT}"
echo "  → Pusť Spotify do '${ZONE_NAME}' a nalezená LARA rádia se přepnou na audio zónu."
echo "  → Po ${IDLE_TIMEOUT}s bez Spotify se LARA zase vypnou."
echo "=================================================================="

# --- SlimProto controller (discovery + push na LARA při Spotify-active) ---
CTRL_PID=""
if command -v python3 >/dev/null 2>&1; then
  log "Spouštím SlimProto controller (režim: ${CMODE})..."
  python3 /opt/lr3ctl/controller.py &
  CTRL_PID=$!
else
  log "python3 chybí — SlimProto controller přeskočen."
fi

# --- Čisté ukončení ---
terminate() {
  log "Zastavuji..."
  [ -n "${CTRL_PID}" ] && kill "${CTRL_PID}" 2>/dev/null
  [ -n "${LIQ_PID}" ] && kill "${LIQ_PID}" 2>/dev/null
  kill "${ICECAST_PID}" 2>/dev/null
  wait 2>/dev/null
  exit 0
}
trap terminate SIGTERM SIGINT

# Drž PID 1 naživu, dokud běží potomci.
wait
