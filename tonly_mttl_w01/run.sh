#!/usr/bin/with-contenv bashio
# Translate the add-on's UI options into the environment variables the
# controller already reads. The controller is used VERBATIM - no fork of
# server.py - so every option maps onto an env var it understands today.
#
#   TONLY_MTTL_W01_MODE               auto | mttl | tcp | sim (mttl = real hardware only)
#   TONLY_MTTL_W01_LISTEN              HTTP port (default 8099, fixed in code)
#   TONLY_MTTL_W01_DEVICE_PORT         TCP 10086, the port the strip dials
#   TONLY_MTTL_W01_POLL                seconds between state reads
#   TONLY_MTTL_W01_PROTECT             firmware channels, legacy global list
#   TONLY_MTTL_W01_PROTECT_BY_DEVICE   DEV=ch,ch;DEV2=ch  (per strip)
#   TONLY_MTTL_W01_HISTORY_INTERVAL    seconds between history samples
#   TONLY_MTTL_W01_HISTORY_KEEP_H      hours of history to retain

export TONLY_MTTL_W01_STATE="/config/tonly-mttl-w01"
export TONLY_MTTL_W01_LISTEN="8099"
export TONLY_MTTL_W01_DEVICE_PORT="10086"

export TONLY_MTTL_W01_MODE="$(bashio::config 'mode')"
export TONLY_MTTL_W01_POLL="$(bashio::config 'poll')"
export TONLY_MTTL_W01_PROTECT="$(bashio::config 'protect')"
export TONLY_MTTL_W01_PROTECT_BY_DEVICE="$(bashio::config 'protect_by_device')"
export TONLY_MTTL_W01_HISTORY_INTERVAL="$(bashio::config 'history_interval')"
export TONLY_MTTL_W01_HISTORY_KEEP_H="$(bashio::config 'history_keep_h')"

# PROTECT is intentionally NOT emptied when the per-strip map is set. The
# controller resolves an absent or unparseable map back to this legacy list on
# purpose, so that a typo in the per-strip setting lands on "something is
# protected" rather than on "nothing is protected". Blanking this line would
# remove the only fail-safe between the two options.
if [ -z "${TONLY_MTTL_W01_PROTECT}" ] && [ -n "${TONLY_MTTL_W01_PROTECT_BY_DEVICE}" ]; then
  bashio::log.warning "protect_by_device is set but the legacy 'protect' list is empty. A typo in the per-strip setting would then protect NOTHING."
fi

if [ "${TONLY_MTTL_W01_MODE}" = "sim" ]; then
  bashio::log.warning "mode=sim: no real hardware is involved. This is the built-in simulator."
fi

# One-shot migration from the pre-rename state dir (legacy name, kept here
# only so an update does not orphan protection/timers/history silently).
# Copies old contents forward when the new dir is empty/absent.
LEGACY_STATE_DIR="/config/moshtarak-wifi"
if [ -d "${LEGACY_STATE_DIR}" ] && [ ! -f "${TONLY_MTTL_W01_STATE}/config.json" ] && [ ! -f "${TONLY_MTTL_W01_STATE}/history.db" ]; then
  mkdir -p "${TONLY_MTTL_W01_STATE}"
  cp -an "${LEGACY_STATE_DIR}/." "${TONLY_MTTL_W01_STATE}/" 2>/dev/null || true
  bashio::log.warning "migrated prior state from ${LEGACY_STATE_DIR} to ${TONLY_MTTL_W01_STATE}; verify protection and timers."
fi

mkdir -p "${TONLY_MTTL_W01_STATE}"

bashio::log.info "starting tonly-mttl-w01 (mode=${TONLY_MTTL_W01_MODE}, strip dials TCP ${TONLY_MTTL_W01_DEVICE_PORT})"

# exec so server.py is PID 1's child-replacement and receives SIGTERM itself.
cd /opt/tonly-mttl-w01
exec python3 server.py