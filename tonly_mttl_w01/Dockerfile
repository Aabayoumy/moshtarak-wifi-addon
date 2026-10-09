# TONLY MTTL-W01 WiFi - Home Assistant app/add-on
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
    TONLY_MTTL_W01_STATE=/config/tonly-mttl-w01

COPY rootfs/ /opt/tonly-mttl-w01/
COPY run.sh /opt/tonly-mttl-w01/run.sh

# Fail the BUILD on a syntax error rather than crash-looping at container start,
# where the Supervisor log is far less obvious about the cause.
RUN python3 -m py_compile /opt/tonly-mttl-w01/server.py /opt/tonly-mttl-w01/adapters.py \
 && rm -rf /opt/tonly-mttl-w01/__pycache__ \
 && chmod +x /opt/tonly-mttl-w01/run.sh

# Actually start the controller and ask it for /api/health, inside the image,
# before anybody installs it. A successful build does not imply a startable app:
# a base image is free to ship a trimmed Python, and a controller can start yet
# fail to bind or serve.
#
# build_verify.py is copied to /tmp and deleted in the same RUN, so it is not
# part of the running image (the COPY layer that briefly held it remains in the
# layer history, ~4kB, which is the price of a readable check).
COPY build_verify.py /tmp/build_verify.py
RUN python3 /tmp/build_verify.py && rm -f /tmp/build_verify.py

# NOTE: no `USER` instruction, deliberately. An earlier version created an
# unprivileged `tonly` user and set USER to it, and the app still would not
# start - for two independent reasons:
#
#   1. s6-overlay v3 (what the Home Assistant base images use) requires the
#      container to begin as root so it can set up supervision and then drop
#      privileges itself. With USER set, the app died instantly with
#      "s6-overlay-suexec: fatal: can only run as pid 1".
#   2. TONLY_MTTL_W01_STATE is /config/tonly-mttl-w01, and /config is a bind
#      mount of the shared, root-owned Home Assistant configuration directory
#      supplied by the Supervisor at run time. An unprivileged process cannot
#      create that directory, so the controller would have failed on its state
#      database. Keeping history there is deliberate: it is what puts
#      history.db inside Home Assistant's own backups.
#
# So the app runs as root, as most Home Assistant apps do. The controller needs
# no capabilities, listens only on the published callback port and an
# unpublished internal port, and executes no user-supplied code.

WORKDIR /opt/tonly-mttl-w01

CMD [ "/opt/tonly-mttl-w01/run.sh" ]