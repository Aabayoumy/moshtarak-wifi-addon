#!/usr/bin/env python3
"""Build-time gate: prove this base image can actually run the controller.

Run from the Dockerfile during `docker build`, then deleted in the same layer.
It exists because "the image built" says almost nothing about "the app will
start". Two real failures got past that gap:

  * a trimmed or absent Python in the base image, where every stdlib module the
    controller imports is missing and the first `import` line kills it;
  * a controller that starts but cannot serve, which restarts in a loop with a
    log line that does not name the cause.

Neither is visible from `docker build` succeeding. Both are visible from this.

The port and the state directory are deliberately build-local: the real
MOSHTARAK_WIFI_STATE is /config/tonly-mttl-w01, which does not exist yet at
build time because /config is a bind mount supplied by the Supervisor when the
app runs. Getting that wrong is why the build uses a throwaway directory.

Exit code 0 means the controller answered /api/health. Anything else fails the
build, which is the only place a maintainer will actually read about it.

It can also be run outside a build, which is how it was tested before being
trusted with the build:

    MOSHTARAK_RUNTIME_DIR=tonly_mttl_w01 python3 tonly_mttl_w01/build_verify.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

# The exact stdlib surface the controller imports. Verified from source, not
# guessed: json, os, re, socket, socketserver, sqlite3, sys, threading, time,
# and http.server for the API.
REQUIRED = (
    "json", "os", "re", "socket", "socketserver", "sqlite3", "sys",
    "threading", "time",
)

RUNTIME_DIR = os.environ.get("MOSHTARAK_RUNTIME_DIR", "/opt/tonly-mttl-w01")
TEST_PORT = int(os.environ.get("MOSHTARAK_GATE_PORT", "8479"))


def fail(message: str) -> None:
    # Flush stdout first: in a build log the interleaving of the two streams
    # otherwise reads as though the failure came before the checks it refers to.
    sys.stdout.flush()
    print(f"BUILD GATE FAILED: {message}", file=sys.stderr)
    sys.stderr.flush()
    raise SystemExit(1)


def main() -> int:
    print("== 1. the base image's Python has every module the controller needs")
    for name in REQUIRED:
        try:
            __import__(name)
        except ImportError as err:
            fail(f"base image Python cannot import {name!r}: {err}")
    try:
        from http.server import ThreadingHTTPServer  # noqa: F401
    except ImportError as err:
        fail(f"base image Python has no http.server: {err}")
    print(f"   ok: {', '.join(REQUIRED)} + http.server")

    if not os.path.isfile(os.path.join(RUNTIME_DIR, "server.py")):
        fail(f"server.py is not in {RUNTIME_DIR} - did the COPY land?")

    print("\n== 2. the controller actually starts and serves /api/health")
    state_dir = tempfile.mkdtemp(prefix="buildgate-")
    env = dict(os.environ)
    # sim, because there is no strip and never will be during a build. auto
    # would try to open the real callback port, which is meaningless here.
    env["MOSHTARAK_WIFI_MODE"] = "sim"
    env["MOSHTARAK_WIFI_LISTEN"] = str(TEST_PORT)
    env["MOSHTARAK_WIFI_STATE"] = state_dir

    proc = subprocess.Popen(
        [sys.executable, "server.py"],
        cwd=RUNTIME_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    health = None
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                output = proc.stdout.read() if proc.stdout else ""
                fail(
                    "the controller exited during startup "
                    f"(exit {proc.returncode}):\n{output[-2000:]}"
                )
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{TEST_PORT}/api/health", timeout=3
                ) as resp:
                    health = json.loads(resp.read())
                    break
            except (urllib.error.URLError, OSError):
                time.sleep(0.4)

        if health is None:
            output = ""
            if proc.poll() is not None and proc.stdout:
                output = proc.stdout.read()
            fail(
                "the controller never answered /api/health within 30s"
                + (f":\n{output[-2000:]}" if output else "")
            )

        print(f"   /api/health -> ok={health.get('ok')}")

        if not health.get("ok"):
            fail(f"/api/health reported ok={health.get('ok')!r}")

        mode = health.get("config", {}).get("mode")
        if mode != "sim":
            fail(f"the mode from the environment did not reach the controller: {mode!r}")

        # The measured socket->channel order. If this is wrong, switching a
        # socket drives a different physical outlet, so it is worth failing a
        # build over rather than discovering it with real hardware attached.
        order = health.get("config", {}).get("order")
        if order != [2, 3, 4, 1]:
            fail(f"unexpected socket->channel order {order!r}, expected [2, 3, 4, 1]")

        if proc.poll() is not None:
            fail("the controller exited between answering and being inspected")
    finally:
        # Terminate by PID. Never pkill -f server.py: that pattern matches any
        # other controller on the machine.
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        shutil.rmtree(state_dir, ignore_errors=True)

    print("\nBUILD GATE PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
