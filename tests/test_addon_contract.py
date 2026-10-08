#!/usr/bin/env python3
"""Prove the add-on's wiring before anyone installs it.

A Docker build cannot be run in this environment, and a `config.yaml` that
parses proves nothing about whether the app will start. What actually breaks an
add-on of this shape is the seam between three files:

    config.yaml  declares options + schema keys
    run.sh       reads them with bashio::config and exports MOSHTARAK_WIFI_* vars
    server.py    reads MOSHTARAK_WIFI_* vars through its env() helper

If a schema key is renamed, or run.sh exports a variable the controller never
reads, or a required option is missing, the app starts and then behaves wrongly
or dies - usually as a restart loop with an unhelpful log. None of that is
visible from the YAML alone.

So this checks the seam directly, and then actually LAUNCHES the controller with
exactly the environment run.sh would produce and asks it for /api/health.

Run:  python3 tests/test_addon_contract.py
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml")
    raise SystemExit(2)

ADDON = Path(__file__).resolve().parent.parent / "tonly_mttl_w01"
CONFIG_YAML = ADDON / "config.yaml"
RUN_SH = ADDON / "run.sh"
DOCKERFILE = ADDON / "Dockerfile"
SERVER = ADDON / "rootfs" / "server.py"
ADAPTERS = ADDON / "rootfs" / "adapters.py"

# Every instruction the add-on's Dockerfile is allowed to use. Anything else in
# instruction position means the file does not parse.
DOCKER_INSTRUCTIONS = {
    "ADD", "ARG", "CMD", "COPY", "ENTRYPOINT", "ENV", "EXPOSE", "FROM",
    "HEALTHCHECK", "LABEL", "MAINTAINER", "ONBUILD", "RUN", "SHELL",
    "STOPSIGNAL", "USER", "VOLUME", "WORKDIR",
}

# A port nothing else in this test run is likely to hold.
TEST_PORT = 8477

passed = 0
failed: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    global passed
    if ok:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed.append(label)
        print(f"  FAIL {label}" + (f"  -- {detail}" if detail else ""))


def read_config() -> dict:
    return yaml.safe_load(CONFIG_YAML.read_text())


def run_sh_body() -> str:
    text = RUN_SH.read_text()
    # Strip comments so a commented-out line cannot satisfy or break a check.
    lines = [
        ln for ln in text.splitlines()
        if not ln.lstrip().startswith("#")
    ]
    return "\n".join(lines)


def main() -> int:
    cfg = read_config()
    options = cfg.get("options") or {}
    schema = cfg.get("schema") or {}
    body = run_sh_body()

    print("\n1. option / schema / run.sh agreement")

    # Every option key must have a schema entry, or the Supervisor rejects the
    # whole config.
    for key in options:
        check(key in schema, f"option {key!r} has a schema entry")

    for key in schema:
        check(key in options, f"schema key {key!r} has a default in options")

    # Every bashio::config read must name a key that actually exists.
    reads = re.findall(r"""bashio::config\s+['"]([\w]+)['"]""", body)
    check(bool(reads), "run.sh reads at least one option")
    for key in reads:
        check(key in schema, f"run.sh reads {key!r}, which is declared in the schema")

    print("\n2. run.sh -> controller environment")

    # MOSHTARAK_WIFI_<X> exported by run.sh must be one the controller reads.
    # There are two ways it reads one, and a test that only knows about the
    # first reports a false failure on MOSHTARAK_WIFI_STATE - which is read
    # directly with os.environ.get, not through the env() helper:
    #
    #   server.py:60   def env(name, default)  ->  env("MODE", "auto")
    #   server.py:54   STATE_DIR = os.environ.get("MOSHTARAK_WIFI_STATE", ...)
    server_src = SERVER.read_text()
    adapters_src = ADAPTERS.read_text()
    read_envs = set(re.findall(r'env\(\s*"([A-Z_]+)"', server_src))
    read_envs |= set(
        re.findall(
            r'os\.environ\.get\(\s*"MOSHTARAK_WIFI_([A-Z_]+)"',
            server_src + adapters_src,
        )
    )
    exported = set(re.findall(r'export\s+MOSHTARAK_WIFI_([A-Z_]+)=', body))

    for name in sorted(exported):
        check(
            name in read_envs,
            f"MOSHTARAK_WIFI_{name} is read by the controller",
            f"server.py reads: {sorted(read_envs)}",
        )

    # The one variable that must NOT be driven from options: the state dir is
    # fixed to a mapped path so history survives a container rebuild.
    check(
        'MOSHTARAK_WIFI_STATE="/config/moshtarak-wifi"' in body,
        "state dir is pinned to the mapped /config path",
    )
    check(
        "config:rw" in yaml.dump(cfg),
        "config is mapped rw so history.db survives",
    )

    print("\n3. things run.sh must NOT do")

    # Blanking the legacy protect list would delete the only fail-safe between
    # it and the per-strip map.
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("export MOSHTARAK_WIFI_PROTECT="):
            check(
                "bashio::config" in stripped,
                "legacy PROTECT is passed through, not blanked",
                stripped,
            )

    print("\n4. the image contains what it runs")

    for path in (SERVER, ADAPTERS, RUN_SH):
        check(path.is_file(), f"{path.name} is present in the add-on")

    check(
        not list(ADDON.glob("**/__pycache__")),
        "no __pycache__ committed into the add-on payload",
    )
    check(
        not list(ADDON.rglob("*.bak-*")),
        "no .bak- files in the add-on payload",
    )

    print("\n5. the controller actually starts with run.sh's environment")

    env = dict(os.environ)
    for name in sorted(exported):
        if name in ("STATE", "LISTEN", "DEVICE_PORT"):
            continue
        # Feed the schema default exactly as bashio would hand it over.
        value = options.get(name.lower())
        env[f"MOSHTARAK_WIFI_{name}"] = "" if value is None else str(value)

    env["MOSHTARAK_WIFI_MODE"] = "sim"  # no hardware here by definition
    env["MOSHTARAK_WIFI_LISTEN"] = str(TEST_PORT)

    state_dir = tempfile.mkdtemp(prefix="addon-contract-")
    env["MOSHTARAK_WIFI_STATE"] = state_dir

    # Run from a COPY of the runtime files, not from the payload directory.
    # Launching in-place let the interpreter drop __pycache__ into the add-on
    # payload - which is exactly what check 4 exists to catch, so the test was
    # failing its own assertion. It also meant running the suite left the
    # repository dirty, and a stray __pycache__ that later gets committed ends
    # up baked into the built image.
    run_dir = tempfile.mkdtemp(prefix="addon-contract-run-")
    for src in (SERVER, ADAPTERS):
        shutil.copy2(src, Path(run_dir) / src.name)

    proc = subprocess.Popen(
        [sys.executable, "server.py"],
        cwd=run_dir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        health = None
        deadline = time.time() + 20
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{TEST_PORT}/api/health", timeout=3
                ) as resp:
                    import json
                    health = json.loads(resp.read())
                    break
            except (urllib.error.URLError, OSError):
                time.sleep(0.4)

        check(proc.poll() is None, "controller stayed up", "it exited during startup")
        check(health is not None, "controller answered /api/health")

        if health:
            check(bool(health.get("ok")), "health reports ok")
            check(
                health.get("config", {}).get("mode") == "sim",
                "the mode from the options reached the controller",
                f"got mode={health.get('config', {}).get('mode')!r}",
            )
            check(
                health.get("config", {}).get("listen") == TEST_PORT,
                "the listen port reached the controller",
            )
            check(
                health.get("config", {}).get("order") == [2, 3, 4, 1],
                "the measured socket->channel order is intact",
                f"got {health.get('config', {}).get('order')!r}",
            )
    finally:
        # Stop by PID. Never pkill -f server.py: that pattern also matches the
        # other suites and has taken down a live controller before.
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        shutil.rmtree(state_dir, ignore_errors=True)
        shutil.rmtree(run_dir, ignore_errors=True)

    print("\n6. config.yaml is valid for a Supervisor add-on")
    for required in ("name", "version", "slug", "description", "arch", "startup"):
        check(required in cfg, f"config.yaml declares {required}")
    check(
        "image" not in cfg,
        "no `image:` key (it would make install try to pull a nonexistent image)",
    )
    check(
        cfg.get("slug") == "tonly_mttl_w01",
        "slug matches the directory name",
        f"slug={cfg.get('slug')!r}",
    )
    check(
        "watchdog" in cfg,
        "watchdog is set",
    )

    print("\n7. the Dockerfile parses")

    # A Dockerfile that does not parse is invisible to every other check here
    # and to `yaml.safe_load`, but it is fatal at install time - and the install
    # happens on the user's machine, minutes after they click. The failure that
    # motivated this was a multi-line `python3 -c "..."` inside a RUN: Docker
    # continues a line only on an explicit backslash, so the following line was
    # read as an instruction and the build died with
    #   "dockerfile parse error on line 50: unknown instruction: import"
    #
    # So join continuations, then require every remaining logical line to begin
    # with a real Dockerfile instruction.
    raw = DOCKERFILE.read_text().splitlines()
    logical: list[tuple[int, str]] = []
    pending, start = "", 0
    for number, line in enumerate(raw, start=1):
        stripped = line.strip()
        if not pending:
            start = number
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        logical.append((start, (pending + line).strip()))
        pending = ""

    if pending:
        check(False, "Dockerfile has no line ending in a trailing backslash",
              f"starts at line {start}")

    for number, line in logical:
        if not line or line.startswith("#"):
            continue
        word = line.split()[0].upper()
        check(
            word in DOCKER_INSTRUCTIONS,
            f"Dockerfile line {number} starts with an instruction",
            f"got {line[:70]!r}, which would be a parse error at build time",
        )

    # And the specific trap, called out on its own so it cannot regress quietly.
    multi_line_python = [
        (n, ln) for n, ln in enumerate(raw, start=1)
        if re.search(r"""python3?\s+-c\s+["'][^"']*$""", ln.rstrip())
    ]
    check(
        not multi_line_python,
        "no `python -c \"...` left open at end of line in the Dockerfile",
        f"lines {[n for n, _ in multi_line_python]} need to stay on one physical line",
    )

    print("\n8. the app will actually start, not merely build")

    # No USER instruction. s6-overlay v3 - which the Home Assistant base images
    # use - has to start as root so it can set up supervision and drop
    # privileges itself. A Dockerfile that sets USER produced an app that built
    # cleanly, installed cleanly, and then died on every start with
    #   s6-overlay-suexec: fatal: can only run as pid 1
    # which is not a message anyone would connect to a USER line.
    user_lines = [
        (n, ln) for n, ln in enumerate(raw, start=1)
        if re.match(r"\s*USER\s+\S", ln, re.IGNORECASE)
    ]
    check(
        not user_lines,
        "Dockerfile sets no USER (s6-overlay v3 must start as root)",
        f"lines {[n for n, _ in user_lines]}",
    )

    # Nor a `su <somebody> -c` in a RUN: with no such runtime user that fails the
    # build for a reason that reads like a permissions problem rather than a
    # leftover from the abandoned unprivileged design.
    su_lines = [
        (n, ln) for n, ln in enumerate(raw, start=1)
        if re.search(r"\bsu\s+\w+\s+-c\b", ln)
    ]
    check(
        not su_lines,
        "Dockerfile runs nothing through `su <user> -c`",
        f"lines {[n for n, _ in su_lines]}",
    )

    # The build gate must exist AND be invoked, or the checks above are only
    # checking the Dockerfile that was written, not the image it produces.
    gate = ADDON / "build_verify.py"
    check(gate.is_file(), "build_verify.py is present")
    if gate.is_file():
        check(
            "build_verify.py" in DOCKERFILE.read_text(),
            "the Dockerfile COPYs build_verify.py, so the gate actually runs",
        )
        check(
            re.search(r"python3\s+/tmp/build_verify\.py.*rm\s+-f\s+/tmp/build_verify\.py",
                      DOCKERFILE.read_text(), re.DOTALL) is not None,
            "the gate is deleted in the same RUN that runs it, so it is not in the image",
        )

    # MOSHTARAK_WIFI_STATE must stay on the mapped /config path: that is what
    # puts history.db inside Home Assistant's own backups.
    check(
        "MOSHTARAK_WIFI_STATE=/config/moshtarak-wifi" in DOCKERFILE.read_text(),
        "state dir is on the mapped /config path, so history.db is backed up",
    )

    print("\n9. s6-overlay can become PID 1")

    # This one cost two failed installs and a great deal of archaeology.
    #
    # The Home Assistant base image sets ENTRYPOINT ["/init"] (s6-overlay v3).
    # s6-overlay only works as a genuine PID 1, and hands the container CMD to
    # `s6-overlay-suexec`, which refuses to run as anything else. The Supervisor
    # passes the app's `init` key straight to Docker's --init flag and DEFAULTS
    # IT TO TRUE, so Docker injects tini as PID 1, /init becomes a child of
    # tini, and the app dies instantly with:
    #     s6-overlay-suexec: fatal: can only run as pid 1
    #
    # Nothing in the Dockerfile hints at this, and the failure message names
    # neither Docker nor the Supervisor. So assert it here.
    check(
        cfg.get("init") is False,
        "config.yaml sets `init: false`",
        "omitted or truthy means Docker injects tini as PID 1 and s6-overlay's "
        "/init can never run, so the app will not start",
    )

    # If the Dockerfile ever sets its own ENTRYPOINT it would replace /init
    # outright and skip s6 supervision entirely.
    entrypoint_lines = [
        (n, ln) for n, ln in enumerate(raw, start=1)
        if re.match(r"\s*ENTRYPOINT\s+\S", ln, re.IGNORECASE)
    ]
    check(
        not entrypoint_lines,
        "Dockerfile overrides no ENTRYPOINT (the base image's /init must stay)",
        f"lines {[n for n, _ in entrypoint_lines]}",
    )

    # A CMD is fine and is what s6-overlay expects to hand off to.
    check(
        any(re.match(r"\s*CMD\s+\[", ln) for ln in raw),
        "Dockerfile declares an exec-form CMD for s6-overlay to run",
    )

    print(f"\n{passed} passed, {len(failed)} failed")
    if failed:
        print("\nFAILED:")
        for name in failed:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())