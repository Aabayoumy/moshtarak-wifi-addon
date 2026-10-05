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
up:ip:, on TCP 10086, where the Moshtarak-Wifi controller is listening.

The setup dialect is plain text and colon-separated, so the target SSID and the
password must not contain ':'.

Usage:
  provision.py --ssid Karim --password SECRET [--ap TONLY_TAP_3355BA3]
               [--gateway 192.168.1.1] [--controller 192.168.1.105]

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


def passphrase_for(ssid):
    """The device label passphrase is LGU_ plus the suffix after the last '_'."""
    ssid = ssid.strip()
    i = ssid.rfind("_")
    if i < 0 or i == len(ssid) - 1:
        return None
    return "LGU_" + ssid[i + 1:]


def default_gateway():
    """Our default gateway on the interface currently carrying the route."""
    try:
        out = subprocess.run(["ip", "-4", "route", "show", "default"],
                             capture_output=True, text=True, timeout=10).stdout
        m = re.search(r"default via (\d+\.\d+\.\d+\.\d+)", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def command(sock, line, expect_ack=True, timeout=4.0):
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
    return reply


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ssid", required=True, help="home Wi-Fi name")
    ap.add_argument("--password", required=True, help="home Wi-Fi password")
    ap.add_argument("--ap", default="", help="setup SSID, e.g. TONLY_TAP_3355BA3")
    ap.add_argument("--gateway", default="", help="strip IP (setup AP gateway)")
    ap.add_argument("--controller", default="", help="LAN IP the strip should call back")
    a = ap.parse_args()

    ssid, password = a.ssid.strip(), a.password
    if ":" in ssid or ":" in password:
        sys.exit("error: ':' is not supported in SSID/password by this "
                 "text provisioning dialect")
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
        command(s, "up:ip:%s" % a.controller)
        time.sleep(0.4)
        command(s, "up:connect:%s:%s" % (ssid, password))
        command(s, "up:reboot:0", expect_ack=False, timeout=2.0)
    finally:
        try:
            s.close()
        except OSError:
            pass

    print()
    print("Provisioning sent. The strip is rebooting onto '%s'." % ssid)
    print("It should then call %s:10086 on its own. Watch for it with:"
          % a.controller)
    print("    curl -s http://%s:8099/api/state | python3 -m json.tool"
          % a.controller)
    print("    curl -s http://%s:8099/api/health | python3 -m json.tool"
          % a.controller)


if __name__ == "__main__":
    main()