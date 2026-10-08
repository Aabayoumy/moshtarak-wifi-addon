#!/usr/bin/with-contenv bashio
# Translate the add-on's UI options into the environment variables the
# controller already reads. The controller is used VERBATIM - no fork of
# server.py - so every option maps onto an env var it understands today.
#
#   MOSHTARAK_WIFI_MODE               auto | tcp | sim
#   MOSHTARAK_WIFI_LISTEN              HTTP port (default 8099, fixed in code)
#   MOSHTARAK_WIFI_DEVICE_PORT         TCP 10086, the port the strip dials
#   MOSHTARAK_WIFI_POLL                seconds between state reads
#   MOSHTARAK_WIFI_PROTECT             firmware channels, legacy global list
#   MOSHTARAK_WIFI_PROTECT_BY_DEVICE   DEV=ch,ch;DEV2=ch  (per strip)
#   MOSHTARAK_WIFI_HISTORY_INTERVAL    seconds between history samples
#   MOSHTARAK_WIFI_HISTORY_KEEP_H      hours of history to retain

export MOSHTARAK_WIFI_STATE="/config/moshtarak-wifi"
export MOSHTARAK_WIFI_LISTEN="8099"
export MOSHTARAK_WIFI_DEVICE_PORT="10086"

export MOSHTARAK_WIFI_MODE="$(bashio::config 'mode')"
export MOSHTARAK_WIFI_POLL="$(bashio::config 'poll')"
export MOSHTARAK_WIFI_PROTECT="$(bashio::config 'protect')"
export MOSHTARAK_WIFI_PROTECT_BY_DEVICE="$(bashio::config 'protect_by_device')"
export MOSHTARAK_WIFI_HISTORY_INTERVAL="$(bashio::config 'history_interval')"
export MOSHTARAK_WIFI_HISTORY_KEEP_H="$(bashio::config 'history_keep_h')"

# PROTECT is intentionally NOT emptied when the per-strip map is set. The
# controller resolves an absent or unparseable map back to this legacy list on
# purpose, so that a typo in the per-strip setting lands on "something is
# protected" rather than on "nothing is protected". Blanking this line would
# remove the only fail-safe between the two options.
if [ -z "${MOSHTARAK_WIFI_PROTECT}" ] && [ -n "${MOSHTARAK_WIFI_PROTECT_BY_DEVICE}" ]; then
  bashio::log.warning "protect_by_device is set but the legacy 'protect' list is empty. A typo in the per-strip setting would then protect NOTHING."
fi

if [ "${MOSHTARAK_WIFI_MODE}" = "sim" ]; then
  bashio::log.warning "mode=sim: no real hardware is involved. This is the built-in simulator."
fi

mkdir -p "${MOSHTARAK_WIFI_STATE}"

bashio::log.info "starting moshtarak-wifi (mode=${MOSHTARAK_WIFI_MODE}, strip dials TCP ${MOSHTARAK_WIFI_DEVICE_PORT})"

# exec so server.py is PID 1's child-replacement and receives SIGTERM itself.
cd /opt/moshtarak-wifi
exec python3 server.py