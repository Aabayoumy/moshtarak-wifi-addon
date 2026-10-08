#!/usr/bin/env python3
"""
Provision a TONLY / MTTL-W01 strip onto a Wi-Fi network, then let our
controller take over.

The strip boots into a setup access point named TONLY_TAP_<id> / ONLY_TAP_<id>
with WPA2 passphrase LGU_<id>. While joined to it, the device answers TCP 30300
and accepts three line commands:

    up:ip:<controller-lan-ip>\r\n        tell it where to call back
    up:connect:<ssid>:<password>\r\n     tell it which Wi-Fi to join
    up:reboot:0\r\n                     apply and restart

After that the strip reboots onto <ssid> and dials back to the address given in
up:ip:, on TCP 10086, where the MTTL-W01 WiFi controller is listening.

The setup dialect is plain text and colon-separated, so the target SSID and the
password must not contain ':'.

The working reference for this flow is the vendor's own iPhone app
(MTL-W01-iPhone / Provisioner.swift): reply to `up:ip:` must contain `ip_ok`,
reply to `up:connect:` must contain `connect_ok`, and the device reboots itself
after accepting the credentials - the iOS app never sends `up:reboot:0`. This
script mirrors that, and only sends `up:reboot:0` when asked with --reboot
(the no-reboot default is the proven path).

Usage:
  provision.py --ssid IOT --password SECRET [--ap TONLY_TAP_3355BA3]
               [--gateway 192.168.1.1] [--controller 192.168.1.105]
               [--reboot]

--gateway is the setup access point's gateway address, which is the strip's own
IP. It is auto-detected from the default route when possible.
"""
import argparse
import re
import socket
import subprocess
import sys
import time

SETUP_PORT = 30300
RE_AP = re.compile(r"^(?:TONLY_TAP_|ONLY_TAP_)(.+)$", re.I)

# Curly/smart quote pairs that end up in an SSID or password when the value is
# typed on an iPhone, pasted from Notes/Mail, or mangled by smart-quote
# autocorrect. The strip treats them as literal characters: it will try to join
# a network literally named "\u201cIOT\u201d" and fail forever. Silently.
_SMART_QUOTES = {
    "\u201c",  # left double quotation mark
    "\u201d",  # right double quotation mark
    "\u2018",  # left single quotation mark
    "\u2019",  # right single quotation mark
    "\u2032",  # prime
    "\u2033",  # double prime
}
_QUOTE_CHARS = _SMART_QUOTES | {'"', "'"}
# Curly quotes come in left/right pairs with DIFFERENT codepoints, so a value
# like \u201cIOT\u201d must be recognised as one matching pair.
_QUOTE_PAIRS = (
    ("\u201c", "\u201d"),  # “ ”
    ("\u2018", "\u2019"),  # ‘ ’
    ('"', '"'),
    ("'", "'"),
)
_RE_NON_ASCII = re.compile(r"[^\x20-\x7e]")


def passphrase_for(ssid):
    """The device label passphrase is LGU_ plus the suffix after the last '_'."""
    ssid = ssid.strip()
    i = ssid.rfind("_")
    if i < 0 or i == len(ssid) - 1:
        return None
    return "LGU_" + ssid[i + 1:]


def _normalize_quotes(value, what):
    """Strip surrounding quote pairs and make the change loud.

    The test that kept failing for the user ("still not connect to my wifi")
    was caused by exactly this: the SSID/password were sent with U+201C/U+201D
    wrapped around them (or a leftover pair of straight quotes), so the strip
    spent its life hunting for a network literally named "\u201cIOT\u201d" (or
    '"IOT"'). Quoting a value should mean "the value is IOT", not "the value is
    \u201cIOT\u201d". Remove one matching pair of quote characters from each end
    of the value; anything left inside is sent as-is.
    """
    out = value
    changed = False
    while True:
        stripped = False
        for left, right in _QUOTE_PAIRS:
            if out.startswith(left) and out.endswith(right) and len(out) >= 2:
                out = out[len(left):len(out) - len(right)]
                changed = True
                stripped = True
                break
        if not stripped or not out:
            break
    if changed:
        print("  NOTE: %s was wrapped in quotes; sending %r instead of %r"
              % (what, out, value))
    return out


def _reject_bad_chars(value, what):
    """Reject characters the text dialect cannot carry, before any socket I/O."""
    if ":" in value:
        sys.exit("error: ':' is not supported in %s by this text provisioning "
                 "dialect (got %r)" % (what, value))
    m = _RE_NON_ASCII.search(value)
    if m:
        sys.exit("error: non-ASCII character %r in %s; the strip's Wi-Fi "
                 "provisioner only accepts printable ASCII (got %r)"
                 % (m.group(0), what, value))


def default_gateway():
    """Our default gateway on the interface currently carrying the route.

    Linux exposes it via `ip`; macOS (and BSD) do not ship `ip` at all, so fall
    back to `route -n get default` which both understand. This is the second
    thing that used to fail on a Mac: 'No --gateway' aborted because `ip` was
    missing even though the correct answer was one syscall away.
    """
    candidates = (
        ("ip", "-4", "route", "show", "default"),
        ("route", "-n", "get", "default"),
    )
    for argv in candidates:
        try:
            out = subprocess.run(argv, capture_output=True,
                                 text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            continue  # tool absent or failed; try the next one
        m = re.search(r"default via (\d+\.\d+\.\d+\.\d+)", out)
        if not m:
            m = re.search(r"gateway:\s+(\d+\.\d+\.\d+\.\d+)", out)
        if m:
            return m.group(1)
    return None


def command(sock, line, expect_ack=True, timeout=4.0, expect=None, what="command"):
    """Send one text command and return the reply.

    With expect_ack=True a missing reply is fatal. With expect=<substring>,
    mirroring the working iPhone app, the reply must also contain that token
    (e.g. 'ip_ok' / 'connect_ok') or the submission is treated as rejected:
    silently printing whatever the strip said and declaring success is exactly
    how the first provisioning run went wrong.
    """
    sock.sendall((line + "\r\n").encode())
    sock.settimeout(timeout)
    try:
        data = sock.recv(512)
    except socket.timeout:
        data = b""
    if expect_ack and not data:
        raise RuntimeError("no reply to %r (device did not acknowledge)" % line)
    reply = data.decode("utf-8", "replace").strip()
    print("  -> %-46s <- %s" % (line, reply or "(no ack, timeout)"))
    if expect and expect not in reply:
        raise RuntimeError("%s was rejected by the strip: expected reply to "
                           "contain %r, got %r (strip may have joined the wrong "
                           "network, or the SSID/password differ from the "
                           "network you are named after)" % (what, expect, reply))
    return reply


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ssid", required=True, help="home Wi-Fi name")
    ap.add_argument("--password", required=True, help="home Wi-Fi password")
    ap.add_argument("--ap", default="", help="setup SSID, e.g. TONLY_TAP_3355BA3")
    ap.add_argument("--gateway", default="", help="strip IP (setup AP gateway)")
    ap.add_argument("--controller", default="", help="LAN IP the strip should call back")
    ap.add_argument("--reboot", action="store_true",
                    help="also send up:reboot:0 (optional; the iOS-app-proven "
                         "flow omits it because the strip reboots itself)")
    a = ap.parse_args()

    ssid, password = a.ssid.strip(), a.password
    ssid = _normalize_quotes(ssid, "SSID")
    password = _normalize_quotes(password, "Wi-Fi password")
    _reject_bad_chars(ssid, "SSID")
    _reject_bad_chars(password, "Wi-Fi password")
    if not a.controller:
        a.controller = default_gateway()
    if not a.controller:
        sys.exit("error: cannot determine --controller address; pass it explicitly")

    gw = a.gateway
    print("target Wi-Fi      : %s" % ssid)
    print("controller IP     : %s   (strip will dial this on TCP 10086)" % a.controller)
    if a.ap:
        pw = passphrase_for(a.ap)
        print("setup AP          : %s   passphrase %s" % (a.ap, pw))
    print("strip IP          : %s:%d" % (gw or "<autodetect>", SETUP_PORT))
    print()

    if not gw:
        gw = default_gateway()
        if not gw:
            sys.exit("error: cannot detect the setup AP gateway; join the "
                     "setup AP first, then pass --gateway")

    print("connecting to %s:%d ..." % (gw, SETUP_PORT))
    try:
        s = socket.create_connection((gw, SETUP_PORT), timeout=7.0)
    except OSError as e:
        sys.exit("error: cannot reach %s:%d (%s)\n"
                 "  - confirm the strip is powered and blinking (pairing mode)\n"
                 "  - confirm this machine is joined to its setup access point\n"
                 "  - the strip's setup IP may not be the gateway; try --gateway"
                 % (gw, SETUP_PORT, e))

    try:
        s.settimeout(3.5)
        command(s, "up:ip:%s" % a.controller, expect="ip_ok",
                what="controller callback address")
        time.sleep(0.4)
        command(s, "up:connect:%s:%s" % (ssid, password), expect="connect_ok",
                what="Wi-Fi credentials")
        if a.reboot:
            print("  (sending optional reboot command)")
            command(s, "up:reboot:0", expect_ack=False, timeout=2.0)
    except RuntimeError as e:
        sys.exit("error: %s" % e)
    finally:
        try:
            s.close()
        except OSError:
            pass

    print()
    print("Provisioning accepted by the strip (ip_ok + connect_ok).")
    print("The strip is rebooting itself onto '%s'." % ssid)
    print("It should then call %s:10086 on its own. Watch for it with:"
          % a.controller)
    print("    curl -s http://%s:8099/api/state | python3 -m json.tool"
          % a.controller)
    print("    curl -s http://%s:8099/api/health | python3 -m json.tool"
          % a.controller)


if __name__ == "__main__":
    main()