# Moshtarak WiFi - Home Assistant app/add-on
#
# BUILD_FROM is substituted by the Supervisor's add-on build system with the
# correct base image for the architecture being built, e.g.
#   ghcr.io/home-assistant/amd64-base-python:3.13-alpine3.21
# It is the official Home Assistant base image, which is what provides bashio
# and the s6 supervision that run.sh relies on.
ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.13-alpine3.21
FROM ${BUILD_FROM}

# The controller is PURE PYTHON STANDARD LIBRARY (verified: json, os, re,
# socket, socketserver, sqlite3, sys, threading, time, http.server only), so
# there is no pip install and no dependency surface to audit.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MOSHTARAK_WIFI_STATE=/config/moshtarak-wifi

# The controller needs no capabilities whatsoever, so it runs unprivileged.
RUN addgroup -g 1000 moshtarak 2>/dev/null || true \
 && adduser -u 1000 -D -G moshtarak -h /opt/moshtarak-wifi moshtarak 2>/dev/null || true \
 && mkdir -p /config/moshtarak-wifi \
 && chown -R moshtarak:moshtarak /opt/moshtarak-wifi /config

COPY rootfs/ /opt/moshtarak-wifi/
COPY run.sh /opt/moshtarak-wifi/run.sh

# Fail the BUILD on a syntax error rather than crash-looping at container start,
# where the Supervisor log is far less obvious about the cause.
RUN python3 -m py_compile /opt/moshtarak-wifi/server.py /opt/moshtarak-wifi/adapters.py \
 && rm -rf /opt/moshtarak-wifi/__pycache__ \
 && chmod +x /opt/moshtarak-wifi/run.sh

WORKDIR /opt/moshtarak-wifi
USER moshtarak

CMD [ "/opt/moshtarak-wifi/run.sh" ]