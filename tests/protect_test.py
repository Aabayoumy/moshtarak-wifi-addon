#!/usr/bin/env python3
"""
Per-strip protection. Fails SAFE.

The thing being tested is a safety property, not a feature: a lock that silently
stops applying because a device id was typed wrong would let a server outlet be
switched off. So the cases that matter are the failure ones.

  1. protection is per strip, not per channel number
  2. a device absent from the map is deliberately UNPROTECTED (that is what
     Karim asked for on the spare strip, and it must not be accidental)
  3. an unreadable / absent map falls back to the legacy global list, so a
     rollback or a typo leaves the server locked
  4. an unknown device id is announced loudly at start-up
  5. the map survives a save/load round trip
"""
import importlib
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PASS = [0, 0]


def ok(cond, label):
    PASS[0 if cond else 1] += 1
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))


def load(env=None, state=None):
    """Import server.py fresh with a given environment."""
    for mod in ("server", "adapters"):
        sys.modules.pop(mod, None)
    base = {
        "TONLY_MTTL_W01_MODE": "sim",
        "TONLY_MTTL_W01_LISTEN": "18099",
        "TONLY_MTTL_W01_BIND": "127.0.0.1",
        "TONLY_MTTL_W01_STATE": state or tempfile.mkdtemp(),
        "TONLY_MTTL_W01_SIM_STATE": os.path.join(
            state or tempfile.mkdtemp(), "sim.json"),
        "TONLY_MTTL_W01_POLL": "0",
    }
    base.update(env or {})
    old = dict(os.environ)
    os.environ.clear()
    os.environ.update(base)
    try:
        import server
        return importlib.import_module("server")
    finally:
        os.environ.clear()
        os.environ.update(old)


SERVER_A = "2CFDB3355BA3"   # the strip with the server on socket 2
SPARE_B = "AABBCCDDEEFF"    # the empty spare


# --------------------------------------------------------------- parsing
print("\nparsing TONLY_MTTL_W01_PROTECT_BY_DEVICE")
s = load({"TONLY_MTTL_W01_PROTECT_BY_DEVICE":
          "%s=3;%s=1,2" % (SERVER_A, SPARE_B)})
ok(s.CONFIG["protect_by_device"] == {SERVER_A: [3], SPARE_B: [1, 2]},
   "parses two devices, one with two channels")
ok(s.CONFIG["protect_by_device"][SERVER_A.upper()] == [3],
   "device id is normalised to upper case")
ok(_try := load({"TONLY_MTTL_W01_PROTECT_BY_DEVICE": "  "}).CONFIG[
    "protect_by_device"] == {}, "blank value yields an empty map")
ok(load({"TONLY_MTTL_W01_PROTECT_BY_DEVICE": "garbage"}).CONFIG[
    "protect_by_device"] == {}, "unparseable value yields an empty map")
ok(load({"TONLY_MTTL_W01_PROTECT_BY_DEVICE": "%s=9" % SERVER_A}).CONFIG[
    "protect_by_device"] == {},
   "channel 9 does not exist, so the whole entry is rejected")
ok(load({"TONLY_MTTL_W01_PROTECT_BY_DEVICE": "%s=3,4,x" % SPARE_B}).CONFIG[
    "protect_by_device"] == {SPARE_B: [3, 4]},
   "a junk channel is dropped but the good ones survive")

# ------------------------------------------------------------ the mapping
print("\nthe measured socket/channel order still applies")
s = load({"TONLY_MTTL_W01_PROTECT_BY_DEVICE": "%s=2" % SERVER_A})
ok(s.channel_to_socket(2) == 2,
   "firmware channel 2 is physical socket 2 (the server)")

# -------------------------------------------------- per-strip behaviour
print("\nprotection is per strip")
s = load({"TONLY_MTTL_W01_PROTECT_BY_DEVICE": "%s=2" % SERVER_A})
ok(s.protected_channels_for(SERVER_A) == [2], "the server strip locks channel 2")
ok(s.is_protected_channel(2, SERVER_A) is True,
   "socket 2 on the server strip is protected")
ok(s.protected_channels_for(SPARE_B) == [],
   "the spare strip has no locks, which is what was asked for")
ok(s.is_protected_channel(2, SPARE_B) is False,
   "socket 2 on the SPARE is NOT protected - the whole point of the change")

print("\nprotection tracks the strip you ask about")
s = load({"TONLY_MTTL_W01_PROTECT_BY_DEVICE":
          "%s=2;%s=1" % (SERVER_A, SPARE_B)})
ok(s.is_protected_channel(1, SPARE_B) is True and
   s.is_protected_channel(2, SPARE_B) is False,
   "the spare locks only its own channel 1")
ok(s.is_protected_channel(2, SERVER_A) is True and
   s.is_protected_channel(1, SERVER_A) is False,
   "the two strips do not share each other's locks")
ok(s.channel_to_socket(1) == 1,
   "and channel 1 is physical socket 1 on this model")

# ------------------------------------------------------------ fails safe
print("\nFAILS SAFE: an absent map keeps the legacy global protection")
s = load({"TONLY_MTTL_W01_PROTECT": "2"})
ok(s.CONFIG["protect_by_device"] == {}, "no map configured")
ok(s.is_protected_channel(2, SERVER_A) is True,
   "server strip still protected by the legacy list")
ok(s.is_protected_channel(2, SPARE_B) is True,
   "spare over-protected rather than under-protected (the safe direction)")
ok(s.is_protected_channel(2, None) is True,
   "an unknown device is protected, not assumed safe")

print("\nFAILS SAFE: a typo'd device id does not unlock the server")
s = load({"TONLY_MTTL_W01_PROTECT_BY_DEVICE":
          "2CFDB3355BA4=3"})          # one character wrong
ok(s.CONFIG["protect_by_device"] == {"2CFDB3355BA4": [3]},
   "the typo is parsed (it is a valid id, just the wrong one)")
ok(s.protected_channels_for(SERVER_A) == [],
   "the real strip gets no locks from a typo'd entry")
# ...so the startup check is the thing that must catch it. Verify it speaks up.
import io
import contextlib
buf = io.StringIO()
s.CONFIG["protect_by_device"] = {"2CFDB3355BA4": [3]}
s.list_devices = lambda: [{"devid": SERVER_A, "connected": True}]
with contextlib.redirect_stdout(buf):
    s._warn_about_protection_typos()
out = buf.getvalue()
ok("MATCHES NO KNOWN STRIP" in out and "2CFDB3355BA4" in out,
   "start-up shouts that the protected id matches no known strip")

print("\nFAILS SAFE: BOTH settings present - the map decides, the list is the net")
# This is the live configuration. The legacy list is deliberately LEFT IN PLACE
# as a fallback, because blanking it would remove the only thing standing between
# a typo in the new setting and an unprotected server outlet: if the map ever
# fails to parse, protection would fall back to an EMPTY list, not to [3].
s = load({"TONLY_MTTL_W01_PROTECT": "2",
          "TONLY_MTTL_W01_PROTECT_BY_DEVICE": "%s=2" % SERVER_A})
ok(s.CONFIG["protect"] == [2], "the legacy list is still there as the net")
ok(s.is_protected_channel(2, SERVER_A) is True,
   "the server strip is protected via the map")
ok(s.is_protected_channel(2, SPARE_B) is False,
   "the spare is still NOT protected - the map, not the list, decides")
# ...and if the map is what breaks, the net catches it.
s.CONFIG["protect_by_device"] = {}
ok(s.is_protected_channel(2, SERVER_A) is True,
   "with the map gone the legacy list takes over and the server is STILL locked")
ok(s.is_protected_channel(2, SPARE_B) is True,
   "the spare is then over-protected, which is the harmless direction")

print("\nno false alarm when only the simulator is listed")
# In auto mode the adapter lists the in-process simulator as device "SIM"
# alongside real strips. Treating that placeholder as a known strip made every
# restart shout that the server's protection id was a typo.
s = load({"TONLY_MTTL_W01_PROTECT_BY_DEVICE": "%s=2" % SERVER_A})
s.list_devices = lambda: [{"devid": "SIM", "simulated": True,
                           "connected": True}]
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    s._warn_about_protection_typos()
ok(buf.getvalue().strip() == "",
   "the simulator alone does not trigger a typo warning")

s.list_devices = lambda: [{"devid": "SIM", "simulated": True,
                           "connected": True},
                          {"devid": SERVER_A, "connected": True}]
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    s._warn_about_protection_typos()
out = buf.getvalue()
ok("locks socket [2]" in out and "MATCHES NO KNOWN STRIP" not in out,
   "once the real strip is listed it reports the lock, not a typo")

print("\nFAILS SAFE: an unknown device is treated as protected")
s = load({"TONLY_MTTL_W01_PROTECT_BY_DEVICE": "%s=2" % SERVER_A})
ok(sorted(s.protected_channels_for("")) == [2],
   "no device named -> union of all locks, not an empty set")
ok(sorted(s.protected_channels_for(None)) == [2], "same for None")

# ------------------------------------------------------ save/load round trip
print("\nthe map survives a save and reload")
state = tempfile.mkdtemp()
s = load({"TONLY_MTTL_W01_PROTECT_BY_DEVICE":
          "%s=3;%s=1" % (SERVER_A, SPARE_B), "TONLY_MTTL_W01_STATE": state})
s._save_config()
written = json.load(open(os.path.join(state, "config.json")))
ok(written.get("protect_by_device") == {SERVER_A: [3], SPARE_B: [1]},
   "config.json records both strips")
s2 = load({"TONLY_MTTL_W01_STATE": state})
s2.CONFIG["protect_by_device"] = {}
s2.load_config()
ok(s2.CONFIG["protect_by_device"] == {SERVER_A: [3], SPARE_B: [1]},
   "and it is restored on reload")

print("\nFAILS SAFE: a corrupt saved map does not clear protection")
state = tempfile.mkdtemp()
with open(os.path.join(state, "config.json"), "w") as fh:
    json.dump({"protect_by_device": {"2CFDB3355BA3": ["nonsense"]}}, fh)
s3 = load({"TONLY_MTTL_W01_PROTECT": "2", "TONLY_MTTL_W01_STATE": state})
s3.load_config()
ok(s3.CONFIG["protect_by_device"] == {},
   "an unusable saved map is discarded, leaving the legacy list in charge")
ok(s3.is_protected_channel(2, SERVER_A) is True,
   "so the server outlet is still locked")

print("\n%d passed, %d failed" % (PASS[0], PASS[1]))
sys.exit(1 if PASS[1] else 0)