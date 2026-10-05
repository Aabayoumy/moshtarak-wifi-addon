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
#
# These are NOT `|| true`. An earlier version swallowed a failure here, and the
# result was a layer that "built fine" but produced a container whose `USER
# moshtarak` does not exist - so it failed to start, with the reason visible
# nowhere except a Supervisor log line about an unknown user. A build that
# cannot produce a runnable image should fail loudly HERE instead.
RUN addgroup -g 1000 moshtarak \
 && adduser -u 1000 -D -G moshtarak -h /opt/moshtarak-wifi moshtarak \
 && mkdir -p /config/moshtarak-wifi \
 && chown -R moshtarak:moshtarak /opt/moshtarak-wifi /config

COPY rootfs/ /opt/moshtarak-wifi/
COPY run.sh /opt/moshtarak-wifi/run.sh

# Fail the BUILD on a syntax error rather than crash-looping at container start,
# where the Supervisor log is far less obvious about the cause.
RUN python3 -m py_compile /opt/moshtarak-wifi/server.py /opt/moshtarak-wifi/adapters.py \
 && rm -rf /opt/moshtarak-wifi/__pycache__ \
 && chmod +x /opt/moshtarak-wifi/run.sh

# Prove the built image can actually RUN the controller, before anyone installs
# it. Three things are checked that a successful build on its own would not
# catch:
#
#   1. the unprivileged user really exists, so `USER moshtarak` below resolves;
#   2. every stdlib module the controller imports is present in this base image
#      (a base image is free to ship a trimmed Python);
#   3. the controller's HTTP server can actually bind a socket as this user, so
#      a missing capability shows up here rather than as a restart loop.
#
# Each check is one physical line on purpose. Docker continues a RUN only on an
# explicit backslash, so a multi-line `python3 -c "..."` does not parse: the
# second line is read as a fresh instruction and the build dies with
# "dockerfile parse error ... unknown instruction: import". That is not
# hypothetical - it is what an earlier version of this file did, and it was
# only caught because the image was actually built rather than eyeballed.
RUN su moshtarak -c 'python3 -c "import json, os, re, socket, socketserver, sqlite3, sys, threading, time; from http.server import ThreadingHTTPServer; print(\"stdlib ok\")"' \
 && su moshtarak -c 'python3 -c "import socket; s = socket.socket(); s.bind((\"127.0.0.1\", 0)); print(\"can bind:\", s.getsockname()[1]); s.close()"'

WORKDIR /opt/moshtarak-wifi
USER moshtarak

CMD [ "/opt/moshtarak-wifi/run.sh" ]