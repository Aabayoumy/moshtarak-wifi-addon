#!/usr/bin/env python3
"""End-to-end test of the extended MTTL-W01 controller.

Runs the real server.py in simulator mode against a scratch state directory, so
nothing here can touch the real strip or the real server socket. Checks the
physical socket ordering, the protection refusal, timers, history and
diagnostics - the things the app now depends on.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 18099
STATE = tempfile.mkdtemp(prefix="mwtest-")
BASE = "http://127.0.0.1:%d" % PORT

fails = []
passes = []


def check(label, got, want):
    if got == want:
        passes.append(label)
        print("  ok   %-52s %s" % (label, repr(got)))
    else:
        fails.append(label)
        print("  FAIL %-52s got %r want %r" % (label, got, want))


def check_true(label, cond, detail=""):
    check(label + (" [%s]" % detail if detail else ""), bool(cond), True)


def call(path, payload=None, expect=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        method="POST" if payload is not None else "GET")
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    try:
        r = urllib.request.urlopen(req, timeout=10)
        code, text = r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        code, text = e.code, e.read().decode()
    body = json.loads(text) if text else {}
    if expect is not None and code != expect:
        print("  !! %s returned HTTP %s (wanted %s): %s" % (path, code, expect, text[:200]))
    return code, body


env = dict(os.environ)
env.update({
    "MOSHTARAK_WIFI_MODE": "sim",
    "MOSHTARAK_WIFI_LISTEN": str(PORT),
    "MOSHTARAK_WIFI_BIND": "127.0.0.1",
    "MOSHTARAK_WIFI_STATE": STATE,
    # The simulator keeps its own file; point it at the scratch dir too so the
    # test cannot touch a real sim-state.json on the machine running it.
    "MOSHTARAK_WIFI_SIM_STATE": os.path.join(STATE, "sim-state.json"),
    # Firmware channel 3 == physical socket 2 == Karim's server. Protect it the
    # same way the live unit is protected.
    "MOSHTARAK_WIFI_PROTECT": "3",
    "MOSHTARAK_WIFI_POLL": "0",
    "MOSHTARAK_WIFI_HISTORY_INTERVAL": "2",
})
proc = subprocess.Popen([sys.executable, os.path.join(HERE, "server.py")],
                        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True)
try:
    for _ in range(60):
        try:
            urllib.request.urlopen(BASE + "/api/health", timeout=2).read()
            break
        except Exception:
            time.sleep(0.3)
    else:
        raise SystemExit("server did not start")

    print("\n1. physical socket order")
    _, st = call("/api/state", expect=200)
    check("state order is physical 1,2,3,4",
          [s["socket"] for s in st["switches"]], [1, 2, 3, 4])
    check("socket 1 is firmware channel 2", st["switches"][0]["channel"], 2)
    check("socket 2 is firmware channel 3", st["switches"][1]["channel"], 3)
    check("socket 3 is firmware channel 4", st["switches"][2]["channel"], 4)
    check("socket 4 is firmware channel 1", st["switches"][3]["channel"], 1)
    check("protected flag follows the socket, not the channel",
          [s["protected"] for s in st["switches"]], [False, True, False, False])

    print("\n2. protection refuses OFF but allows ON")
    code, b = call("/api/switch/socket/2", {"on": False}, expect=409)
    check("HTTP status for protected socket OFF", code, 409)
    check_true("error names the physical socket", "socket 2" in b.get("error", ""), b.get("error"))
    code, b = call("/api/switch/socket/1", {"on": True}, expect=200)
    check("unprotected socket still switches", code, 200)
    code, _ = call("/api/switch/socket/2", {"on": True}, expect=200)
    check("protected socket may still be switched ON", code, 200)

    print("\n3. legacy channel path still works (Home Assistant)")
    code, b = call("/api/switch/3", {"on": False})
    check("channel 3 OFF refused for HA too", code, 409)
    code, _ = call("/api/switches", {"1": True, "3": False}, expect=200)
    _, b = call("/api/switches", {"1": True, "3": False}, expect=200)
    refused = [r for r in b["results"] if not r.get("ok")]
    check_true("bulk refuses the protected channel", refused, refused)
    check_true("bulk still handles the others",
               any(r.get("ok") for r in b["results"]), b["results"])

    print("\n4. names and order can be set at runtime")
    _, b = call("/api/config", {"names": ["Lamp", "Server", "Free", "Desk LED"]}, expect=200)
    check("names persisted in config response", b["config"]["names"][1], "Server")
    _, st = call("/api/state", expect=200)
    check("state carries the new names",
          [s["name"] for s in st["switches"]], ["Lamp", "Server", "Free", "Desk LED"])
    code, _ = call("/api/config", {"order": [1, 1, 2, 3]}, expect=400)
    check("a bad order permutation is rejected", code, 400)
    _, b = call("/api/config", {"order": [4, 3, 2, 1]}, expect=200)
    _, st = call("/api/state", expect=200)
    check("order change is applied",
          [s["channel"] for s in st["switches"]], [4, 3, 2, 1])
    call("/api/config", {"order": [2, 3, 4, 1]}, expect=200)

    print("\n5. timers")
    _, b = call("/api/timers", {"socket": 1, "at": "22:30", "on": True,
                                "label": "Lamp evening"}, expect=200)
    check("timer added", len(b["timers"]), 1)
    tid = b["timers"][0]["id"]
    check("timer resolves its firmware channel", b["timers"][0]["channel"], 2)
    check("timer counts down", isinstance(b["timers"][0]["in_minutes"], int), True)

    code, _ = call("/api/timers", {"socket": 2, "at": "99:99", "on": True}, expect=400)
    check("a bad time is rejected", code, 400)

    _, b = call("/api/timers", {"socket": 2, "at": "03:00", "on": False}, expect=200)
    off_tid = [t for t in b["timers"] if not t["on"]][0]["id"]
    check("a timer that would kill the server is flagged",
          [t["will_be_refused"] for t in b["timers"] if t["id"] == off_tid], [True])

    _, b = call("/api/timers/toggle", {"id": tid, "enabled": False}, expect=200)
    check("timer can be disabled",
          [t["enabled"] for t in b["timers"] if t["id"] == tid], [False])
    _, b = call("/api/timers/delete", {"id": off_tid}, expect=200)
    check("timer deleted", len(b["timers"]), 1)

    print("\n6. history")
    time.sleep(3)
    _, b = call("/api/history?socket=2&minutes=10&points=50", expect=200)
    check_true("history returns samples for socket 2", b["samples"], len(b["samples"]))
    check_true("history samples carry a raw power value",
               "power_raw" in b["samples"][0], b["samples"][0])
    _, b = call("/api/history?socket=99&minutes=10", expect=200)
    check("unknown socket yields no samples, not an error", b["samples"], [])

    print("\n7. diagnostics")
    _, b = call("/api/diagnostics", expect=200)
    check("diagnostics reports the adapter", b["adapter"], "sim")
    check("diagnostics declares it is a simulator", b["simulated"], True)
    check("diagnostics lists four sockets", len(b["sockets"]), 4)
    check("protection reports the protected physical socket",
          b["protection"]["sockets"], [2])
    check_true("diagnostics includes timer count", "timers" in b, b.get("timers"))
    check_true("diagnostics includes history stats",
               b["history"]["rows"] > 0, b["history"])
    check_true("diagnostics includes uptime", b["uptime_seconds"] >= 0, b["uptime_seconds"])
    # The app explains an empty history window using the server's own sampling
    # period. A hardcoded figure in the app would go quietly wrong the moment
    # the server is reconfigured, so the number has to come from here.
    check_true("diagnostics reports the sampling period",
               b["history_interval_s"] > 0, b.get("history_interval_s"))
    check_true("diagnostics reports how long history is kept",
               b["history_keep_h"] > 0, b.get("history_keep_h"))
    # last_error was a dict of three nulls, which every client read as a live
    # fault. None is the honest answer when nothing has gone wrong.
    check("no last error means null, not a dict of nulls", b["last_error"], None)

    print("\n8. honest telemetry keys")
    _, st = call("/api/state", expect=200)
    s = st["switches"][0]
    for key in ("power_w", "power_raw", "energy_kwh", "temp_c", "state_code",
                "power_verified", "flag3", "flag4", "protected", "channel"):
        check_true("telemetry key '%s' present" % key, key in s, sorted(s))
    check("power is explicitly marked unverified", s["power_verified"], False)
    check("each socket says whether it is simulated", s["simulated"], True)
    check("simulated sockets report no temperature", s["temp_c"], None)

    print("\n9. multiple strips")
    _, st = call("/api/state", expect=200)
    check("one strip is connected", st["devices_connected"], 1)
    check("the strip is named in the response", st["device"], "SIM")
    check("a single strip is not ambiguous", st["ambiguous"], False)
    _, b = call("/api/devices", expect=200)
    check("devices endpoint lists the simulator", len(b["devices"]), 1)
    check("device list marks it connected", b["devices"][0]["connected"], True)
    check("device list carries the device id", b["devices"][0]["devid"], "SIM")

    code, b = call("/api/state?device=NOSUCH", expect=200)
    check("an unknown device reports unreachable, not a crash",
          b["reachable"], False)
    check_true("and says why", "device id" in b.get("error", ""), b.get("error"))
    # An unreachable strip must not also claim to have settled readings.
    check("and does not claim to be settled", b.get("settled"), False)

    print("\n9b. the state says which strip it is about")
    _, st = call("/api/state", expect=200)
    check("settled is reported at all", "settled" in st, True)
    check("a strip that has reported is settled", st.get("settled"), True)
    # Protection must be described against the strip that answered, not against
    # an empty request. Reporting an unnamed lock is how a second strip ends up
    # looking like it has the server's lock on it.
    check("protection names the strip it describes",
          st.get("protection", {}).get("device"), st.get("device"))
    check("the whole per-strip map is visible",
          isinstance(st.get("protection_by_device"), dict), True)

    code, _ = call("/api/switch/socket/1?device=SIM", {"on": True}, expect=200)
    check("a command can be addressed to a specific strip", code, 200)
    _, st = call("/api/state?device=SIM", expect=200)
    check("that strip took the command", st["switches"][0]["on"], True)

    print("\n%d passed, %d failed" % (len(passes), len(fails)))
    if fails:
        for f in fails:
            print("  FAILED: %s" % f)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    shutil.rmtree(STATE, ignore_errors=True)
sys.exit(1 if fails else 0)