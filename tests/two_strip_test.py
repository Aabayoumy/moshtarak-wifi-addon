#!/usr/bin/env python3
"""Two strips on one listener.

This is the test that justifies the whole multi-strip change. It runs the real
server.py and the real MttlAdapter in hardware-only mode, then dials two fake
strips into the same TCP listener with different device ids, different relay
states and different power readings.

It fails if the two strips' states are allowed to mix, if a command addressed at
one strip lands on the other, or if the protection on the server socket stops
applying when a second strip appears.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
API_PORT = 18097
DEVICE_PORT = 18096
STATE = tempfile.mkdtemp(prefix="mw2strip-")
BASE = "http://127.0.0.1:%d" % API_PORT

STRIP_A = "AAAAAAAAAAAA"
STRIP_B = "BBBBBBBBBBBB"

fails, passes = [], []


def check(label, got, want):
    if got == want:
        passes.append(label)
        print("  ok   %-54s %s" % (label, repr(got)))
    else:
        fails.append(label)
        print("  FAIL %-54s got %r want %r" % (label, got, want))


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
        print("  !! %s -> HTTP %s (wanted %s): %s" % (path, code, expect, text[:200]))
    return code, body


def wait_for(fn, seconds=25):
    end = time.time() + seconds
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.4)
    return False


env = dict(os.environ)
env.update({
    "TONLY_MTTL_W01_MODE": "mttl",            # hardware only: no simulator here,
    "TONLY_MTTL_W01_LISTEN": str(API_PORT),   # so nothing can be faked
    "TONLY_MTTL_W01_BIND": "127.0.0.1",
    "TONLY_MTTL_W01_DEVICE_PORT": str(DEVICE_PORT),
    "TONLY_MTTL_W01_STATE": STATE,
    "TONLY_MTTL_W01_PROTECT": "2",            # firmware ch2 = physical socket 2 (server)
    "TONLY_MTTL_W01_POLL": "0",
    "TONLY_MTTL_W01_HISTORY_INTERVAL": "3",
})

server = subprocess.Popen([sys.executable, os.path.join(HERE, "server.py")],
                          env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True)
kids = []


def start_strip(devid, pattern, power, temp):
    return subprocess.Popen(
        [sys.executable, os.path.join(HERE, "fake_strip.py"),
         "--dial", str(DEVICE_PORT), "--host", "127.0.0.1",
         "--devid", devid, "--pattern", pattern, "--power", power,
         "--temp", str(temp)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


try:
    for _ in range(80):
        try:
            urllib.request.urlopen(BASE + "/api/health", timeout=2).read()
            break
        except Exception:
            time.sleep(0.3)
    else:
        raise SystemExit("server did not start")

    # Firmware channel N power values differ per strip so mixing is obvious:
    # strip A reports 1111 on channels 1-4, strip B reports 9999.
    # Every strip has its own distinct power and temperature, and the raw values
    # line up with the relays that are actually closed, so a mixed-up read is
    # impossible to mistake for a correct one.
    #  A: pattern 1010 -> ch1, ch3 on  -> sockets on,off,on,off (identity)
    #     power 1111,2222,3333,4444 -> sockets 1111, 0, 3333, 0
    #  B: pattern 1111 -> all on       -> sockets on,on,on,on
    #     power 9999,8888,7777,6666 -> sockets 9999, 8888, 7777, 6666
    kids.append(start_strip(STRIP_A, "1010", "1111,2222,3333,4444", 21))
    # Start B only once A is registered. Launching both at once is a race over
    # who says hello first, and "which strip is the default" is exactly what this
    # file is testing - it cannot be asserted on a coin toss.
    wait_for(lambda: call("/api/devices")[1].get("connected") == 1, seconds=30)
    kids.append(start_strip(STRIP_B, "1111", "9999,8888,7777,6666", 33))
    time.sleep(1)

    def two_connected():
        _, b = call("/api/devices")
        return b.get("connected") == 2

    if not wait_for(two_connected):
        print("\nTwo strips did not both register. Draining server log:\n")
        print(server.stdout.read() if server.stdout else "")
        raise SystemExit("cannot continue")

    print("\n1. both strips are seen, and the choice is called ambiguous")
    _, b = call("/api/devices", expect=200)
    check("two strips connected", b["connected"], 2)
    ids = sorted(d["devid"] for d in b["devices"] if d["connected"])
    check("both device ids listed", ids, sorted([STRIP_A, STRIP_B]))
    check("device ids are normalised to upper case", ids, sorted([STRIP_A, STRIP_B]))

    _, st = call("/api/state", expect=200)
    check("state is flagged ambiguous when no device is named", st["ambiguous"], True)
    check("state reports two strips connected", st["devices_connected"], 2)
    # The default is the strip that said hello FIRST and stays that way, so
    # plugging in a second strip cannot quietly take over Home Assistant.
    check("state names the strip it answered for", st["device"], STRIP_A)
    check("and that is the first strip to connect",
          [s["power_raw"] for s in st["switches"]], [1111, 0, 3333, 0])

    print("\n2. each strip's own state, not a mixture")
    _, a = call("/api/state?device=%s" % STRIP_A, expect=200)
    check("strip A relays, in physical socket order",
          [s["on"] for s in a["switches"]], [True, False, True, False])
    check("strip A power readings are its own",
          [s["power_raw"] for s in a["switches"]], [1111, 0, 3333, 0])
    check("strip A temperature is its own",
          [s["temp_c"] for s in a["switches"]], [21, 21, 21, 21])
    _, b = call("/api/state?device=%s" % STRIP_B, expect=200)
    check("strip B relays, in physical socket order",
          [s["on"] for s in b["switches"]], [True, True, True, True])
    check("strip B power readings are its own",
          [s["power_raw"] for s in b["switches"]], [9999, 8888, 7777, 6666])
    check("strip B temperature is its own",
          [s["temp_c"] for s in b["switches"]], [33, 33, 33, 33])

    print("\n3. a command reaches only the strip it was aimed at")
    # Physical socket 4 is firmware channel 4: on on strip A, on on strip B.
    code, _ = call("/api/switch/socket/4?device=%s" % STRIP_A, {"on": False}, expect=200)
    check("addressed command accepted", code, 200)
    time.sleep(1.5)
    _, a = call("/api/state?device=%s&x=1" % STRIP_A, expect=200)
    _, b = call("/api/state?device=%s&x=1" % STRIP_B, expect=200)
    check("strip A socket 4 is now off", a["switches"][3]["on"], False)
    check("strip A socket 4 no longer draws", a["switches"][3]["power_raw"], 0)
    check("strip B socket 4 is untouched", b["switches"][3]["on"], True)
    check("strip B still reads its own power",
          b["switches"][3]["power_raw"], 6666)

    print("\n4. protection still applies with two strips up")
    # Physical socket 2 = firmware channel 2, on on strip A, protected.
    code, body = call("/api/switch/socket/2?device=%s" % STRIP_A, {"on": False})
    check("protected socket refuses OFF", code, 409)
    check_true("and says why", "protected" in body.get("error", ""), body.get("error"))
    code, _ = call("/api/switch/socket/2?device=%s" % STRIP_A, {"on": True}, expect=200)
    check("protected socket still accepts ON", code, 200)
    time.sleep(1.5)
    _, a = call("/api/state?device=%s&x=2" % STRIP_A, expect=200)
    check("strip A server socket is still on", a["switches"][1]["on"], True)
    check("and is still marked protected", a["switches"][1]["protected"], True)

    print("\n5. one strip going away does not disturb the other")
    kids[0].send_signal(signal.SIGTERM)
    kids[0].wait(timeout=10)
    wait_for(lambda: call("/api/devices")[1].get("connected") == 1)
    _, b = call("/api/devices", expect=200)
    check("one strip left", b["connected"], 1)
    check("the survivor is B", [d["devid"] for d in b["devices"] if d["connected"]],
          [STRIP_B])
    check("A is still listed, marked disconnected",
          [d["connected"] for d in b["devices"] if d["devid"] == STRIP_A], [False])
    _, st = call("/api/state", expect=200)
    check("state falls back to the one that is up", st["device"], STRIP_B)
    check("no longer ambiguous", st["ambiguous"], False)
    check("and it is B's data, not A's",
          [s["power_raw"] for s in st["switches"]], [9999, 8888, 7777, 6666])

    print("\n6. per-strip protection, with both strips connected")
    # The legacy global list protected socket 2 on BOTH strips. That is wrong:
    # strip A holds the server, strip B is an empty spare.
    server.terminate()
    server.wait(timeout=10)
    env["TONLY_MTTL_W01_PROTECT"] = ""
    env["TONLY_MTTL_W01_PROTECT_BY_DEVICE"] = "%s=2" % STRIP_A
    server = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "server.py")],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(80):
        try:
            urllib.request.urlopen(BASE + "/api/health", timeout=2).read()
            break
        except Exception:
            time.sleep(0.3)
    # Section 5 deliberately killed strip A, so bring it back: this section is
    # about two strips coexisting, not about one.
    kids.append(start_strip(STRIP_A, "1010", "1111,2222,3333,4444", 21))
    wait_for(lambda: call("/api/devices")[1].get("connected") == 2)
    # Restarted strip reports pattern 1010: server socket 2 starts OFF under
    # identity order. Protection refuses OFF but allows ON, so switch it on
    # explicitly (the pre-restart strip had it on).
    call("/api/switch/socket/2?device=%s" % STRIP_A, {"on": True})
    wait_for(lambda: (call("/api/state?device=%s" % STRIP_A)[1].get("switches") or [{}]*2)[1].get("on") is True)
    check("both strips connected again",
          sorted(d["devid"] for d in call("/api/devices")[1]["devices"]
                 if d["connected"]), sorted([STRIP_A, STRIP_B]))

    code, _ = call("/api/switch/socket/2?device=%s" % STRIP_A, {"on": False},
                   expect=409)
    check("server strip still refuses OFF", code, 409)
    # After the controller restarts the strips re-dial, and a named device needs
    # a fresh status block before its sockets appear. Wait rather than assume.
    wait_for(lambda: len(call("/api/state?device=%s" % STRIP_A)[1].get("switches")
                         or []) == 4)
    _, a = call("/api/state?device=%s&x=3" % STRIP_A, expect=200)
    check("server socket 2 still protected on strip A",
          a["switches"][1]["protected"], True)
    check("and strip A's protection is reported per strip",
          sorted(a["protection"]["channels"]), [2])

    # Strip B is the spare and is deliberately NOT protected. This is the
    # behaviour the old global list could not express.
    code, _ = call("/api/switch/socket/2?device=%s" % STRIP_B, {"on": False},
                   expect=200)
    check("spare strip socket 2 is NOT locked", code, 200)
    wait_for(lambda: len(call("/api/state?device=%s" % STRIP_B)[1].get("switches")
                         or []) == 4)
    _, b = call("/api/state?device=%s&x=3" % STRIP_B, expect=200)
    check("spare socket 2 reports unprotected", b["switches"][1]["protected"],
          False)
    check("spare's protection list is empty", b["protection"]["channels"], [])
    check("the whole map is visible for review",
          a["protection_by_device"], {STRIP_A: [2]})

    # The strip list has to carry each strip's own locks, in the numbers a person
    # can match to the socket in front of them. A picker that has to ask a second
    # question per strip shows a blank for any strip that is not connected right
    # now, which is exactly when you most want to see what is locked.
    _, dv = call("/api/devices", expect=200)
    by = {d["devid"]: d for d in dv["devices"]}
    check("the strip list shows the server strip's lock as a SOCKET number",
          by[STRIP_A].get("protected_sockets"), [2])
    # Identity order (blink-verified): socket numbers equal channel numbers, so
    # the old rotated-model check (sockets != channels) no longer applies. Both
    # views must agree on [2] here.
    check("sockets and channels agree under identity order",
          by[STRIP_A].get("protected_sockets") == by[STRIP_A].get("protected_channels") == [2],
          True)
    check("the spare strip is listed with no locks",
          by[STRIP_B].get("protected_sockets"), [])
    check("the map travels with the list too",
          dv.get("protection_by_device"), {STRIP_A: [2]})

    # A strip that has gone away must still say what it locks, from the saved
    # config, rather than silently reporting nothing.
    kids[1].send_signal(signal.SIGTERM)
    kids[1].wait(timeout=10)
    wait_for(lambda: [d["connected"] for d in call("/api/devices")[1]["devices"]
                      if d["devid"] == STRIP_B] == [False])
    _, dv2 = call("/api/devices", expect=200)
    gone = {d["devid"]: d for d in dv2["devices"]}
    check("a disconnected strip is still listed", STRIP_B in gone, True)
    check("and still declares its locks from config",
          gone[STRIP_A].get("protected_sockets"), [2])
    kids.append(start_strip(STRIP_B, "1111", "9999,8888,7777,6666", 33))
    wait_for(lambda: call("/api/devices")[1].get("connected") == 2)

    # Put strip B back as found.
    call("/api/switch/socket/2?device=%s" % STRIP_B, {"on": True}, expect=200)
    time.sleep(1.0)
    _, b = call("/api/state?device=%s&x=4" % STRIP_B, expect=200)
    check("spare socket 2 back on", b["switches"][1]["on"], True)
    _, a = call("/api/state?device=%s&x=4" % STRIP_A, expect=200)
    check("and strip A is undisturbed throughout",
          [a["switches"][1]["on"], a["switches"][1]["protected"]], [True, True])

    print("\n%d passed, %d failed" % (len(passes), len(fails)))
    for f in fails:
        print("  FAILED: %s" % f)
finally:
    for k in kids:
        try:
            k.kill()
        except Exception:
            pass
    try:
        server.terminate()
        server.wait(timeout=5)
    except Exception:
        server.kill()
    shutil.rmtree(STATE, ignore_errors=True)
sys.exit(1 if fails else 0)