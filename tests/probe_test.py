#!/usr/bin/env python3
"""Tests for the read-only measurement probe (/api/probe and Adapter.measure).

Runs the real server in simulator mode, so nothing here can touch the real
strip or the real server socket.

The point of these tests is not that the probe returns a number - it is that it
CANNOT switch anything, and that a simulator never invents a voltage.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 18097
STATE = tempfile.mkdtemp(prefix="probe-")
BASE = "http://127.0.0.1:%d" % PORT

fails, passes = [], []


def check(name, cond, detail=""):
    (passes if cond else fails).append(name)
    print("  %s   %s%s" % ("ok  " if cond else "FAIL", name,
                           ("  [%s]" % (detail,)) if detail else ""))


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return r.status, json.loads(r.read().decode())


env = dict(os.environ)
env.update({
    "MOSHTARAK_WIFI_MODE": "sim",
    "MOSHTARAK_WIFI_LISTEN": str(PORT),
    "MOSHTARAK_WIFI_BIND": "127.0.0.1",
    "MOSHTARAK_WIFI_STATE": STATE,
    "MOSHTARAK_WIFI_SIM_STATE": os.path.join(STATE, "sim-state.json"),
    # Channel 3 == physical socket 2 == the server. Protected exactly as live.
    "MOSHTARAK_WIFI_PROTECT": "3",
    "MOSHTARAK_WIFI_POLL": "0",
})

proc = subprocess.Popen([sys.executable, os.path.join(HERE, "server.py")],
                        env=env, stdout=subprocess.DEVNULL,
                        stderr=subprocess.STDOUT)

try:
    for _ in range(80):
        try:
            get("/api/health")
            break
        except Exception:
            time.sleep(0.25)
    else:
        raise SystemExit("server did not start")

    print("\n1. the probe route answers")
    code, body = get("/api/probe")
    check("route returns 200", code == 200, code)
    check("body is marked ok", body.get("ok") is True)

    print("\n2. a simulator never invents a voltage")
    check("it says it is simulated", body.get("simulated") is True)
    check("voltage is null, not a number", body.get("voltage_v") is None,
          repr(body.get("voltage_v")))
    check("rssi is null, not a number", body.get("rssi_dbm") is None,
          repr(body.get("rssi_dbm")))
    check("per-outlet current is empty", body.get("outlet_ma") == {})
    check("and it says current is unavailable", body.get("current_available") is False)
    check("and it explains why", "simulator" in (body.get("note") or ""))

    print("\n3. the probe cannot switch anything")
    import ast
    tree = ast.parse(open(os.path.join(HERE, "adapters.py")).read())
    fns = [n for n in ast.walk(tree)
           if isinstance(n, ast.FunctionDef) and n.name == "measure"]
    check("measure() exists", bool(fns))
    for fn in fns:
        if not fn.body or not isinstance(fn.body[0], ast.Expr):
            continue
        names = {n.id for n in ast.walk(fn)
                 if isinstance(n, ast.Name) and n.id.startswith("CMD_")}
        for forbidden in ("CMD_ONOFF", "CMD_REBOOT", "CMD_GETINFO_ALL"):
            check("line %d: measure() never uses %s" % (fn.lineno, forbidden),
                  forbidden not in names)
        check("line %d: measure() never calls set_switch" % fn.lineno,
              "set_switch" not in ast.dump(fn))

    print("\n4. switching still works normally afterwards")
    code, st = get("/api/state")
    check("state still served", code == 200)
    check("four sockets present", len(st.get("switches", [])) == 4,
          len(st.get("switches", [])))

    print("\n5. an unknown device is refused, not invented")
    try:
        urllib.request.urlopen(BASE + "/api/probe?device=NOSUCH", timeout=10)
        check("unknown device is rejected", False, "it answered")
    except urllib.error.HTTPError as e:
        check("unknown device is rejected", e.code == 409, e.code)
        check("and says why", "NOSUCH" in e.read().decode())

finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()

print("\n%d passed, %d failed" % (len(passes), len(fails)))
sys.exit(1 if fails else 0)
