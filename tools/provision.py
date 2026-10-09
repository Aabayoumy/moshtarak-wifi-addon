#!/usr/bin/env python3
"""
Provision a TONLY / MTTL-W01 strip onto a Wi-Fi network, then let our
controller take over. One shot, cross-platform (macOS / Linux / Windows),
standard library only.

What it does, in order:

  1. Reads your home Wi-Fi name + password and the controller IP from
     flags, a settings file, or interactive prompts (in that order).
  2. Finds the strip's setup access point (TONLY_TAP_<id> / ONLY_TAP_<id>)
     by scanning, or takes it from --ap, and joins it (passphrase LGU_<id>).
  3. Tells the strip where to call back (up:ip:) and which Wi-Fi to join
     (up:connect:), checking the strip's acknowledgements.
  4. Rejoins your home Wi-Fi and optionally verifies the controller sees it.

The strip boots into its setup access point and answers TCP 30300 with three
line commands (CRLF, ASCII, colon-separated, in order):

    up:ip:<controller-lan-ip>      tell it where to call back (expect ip_ok)
    up:connect:<ssid>:<password>   tell it which Wi-Fi to join (expect connect_ok)
    up:reboot:0                    optional; the strip normally reboots itself

After that the strip reboots onto your Wi-Fi and dials back to the address
given in up:ip:, on TCP 10086, where the controller is listening.

The setup dialect is plain text and colon-separated, so the target SSID and
the password must not contain ':'. The working reference for this flow is the
vendor's own iPhone app (MTL-W01-iPhone / Provisioner.swift): the device
reboots itself after accepting the credentials - the iOS app never sends
up:reboot:0. This script mirrors that, and only sends up:reboot:0 with --reboot
(the no-reboot default is the proven path).

Wi-Fi control per OS (this is the part with no standard-library API, so each
platform shells out to its own native tool):

  macOS    scan: airport(1) when present; join: networksetup -setairportnetwork.
           Newer macOS removed airport(1): scanning then needs --ap, but
           joining still works. May need sudo for the join.
  Linux    nmcli (NetworkManager) for scan, join and rejoin.
  Windows  netsh (scan + join via a temporary WPA2 profile, removed after).

Usage:
  python3 provision.py                                   # interactive, auto-detect
  python3 provision.py --ssid IOT --password SECRET      # non-interactive core
  python3 provision.py --ssid IOT --password SECRET --controller 192.168.1.50 --save

--controller is the Home Assistant host's LAN address (what the strip dials),
NOT the add-on's internal hostname: the strip reaches it from outside over
your LAN. It is remembered in the settings file once given or --save'd.
"""
import argparse
import getpass
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SETUP_PORT = 30300
CALLBACK_PORT = 10086
RE_AP = re.compile(r"^(?:TONLY_TAP_|ONLY_TAP_)(.+)$", re.I)

CONFIG_DIR_NAME = "tonly-mttl-w01"
CONFIG_FILE_NAME = "provision.conf"

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


def strip_is_setup_ap(ssid):
    """True when an SSID looks like a strip waiting to be provisioned."""
    return bool(RE_AP.match((ssid or "").strip()))


def _normalize_quotes(value, what):
    """Strip surrounding quote pairs and make the change loud.

    Quoting a value should mean "the value is IOT", not "the value is
    \u201cIOT\u201d". Remove one matching pair of quote characters from each
    end of the value; anything left inside is sent as-is.
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


# ---------------------------------------------------------------- settings file

def config_path(explicit=None):
    """Where home SSID, password and controller IP are remembered.

    One file on every OS: ~/.config/tonly-mttl-w01/provision.conf, KEY=VALUE
    lines, created mode 600. The password lives there in plaintext (the strip
    needs the literal characters and the standard library has no keyring), so
    treat the file like a secret: it is never printed, only read.
    """
    if explicit:
        return Path(explicit).expanduser()
    return Path.home() / ".config" / CONFIG_DIR_NAME / CONFIG_FILE_NAME


def load_settings(path):
    """Read KEY=VALUE lines; missing file means no saved settings. Never raises."""
    out = {}
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            out[key.strip().upper()] = val.strip()
    except OSError:
        pass
    return out


def save_settings(path, values):
    """Merge values into the settings file, keeping it owner-only."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        merged = load_settings(path)
        merged.update({k.upper(): v for k, v in values.items()})
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join("%s=%s\n" % (k, v) for k, v in sorted(merged.items())),
                       encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass  # Windows: no POSIX modes; the file stays in the user profile
        tmp.replace(path)
    except OSError as exc:
        print("  WARNING: could not save settings to %s (%s)" % (path, exc))


# ------------------------------------------------------------- Wi-Fi backends

class WifiError(RuntimeError):
    """A Wi-Fi operation the platform tool refused or could not do."""


class WifiBackend:
    """Native-tool Wi-Fi control. scan() may raise WifiError; the caller then
    falls back to --ap / manual entry instead of dying."""

    name = "generic"

    def scan(self):
        """Return visible SSIDs (best effort, deduped, in signal order)."""
        raise WifiError("Wi-Fi scanning is not implemented on %s; pass --ap "
                        "with the strip's setup network name" % self.name)

    def current(self):
        """Return the joined SSID, or None when unknown / not on Wi-Fi."""
        return None

    def connect(self, ssid, password=None):
        """Join ssid (open when password is None). Raises WifiError."""
        raise WifiError("Wi-Fi join is not implemented on %s" % self.name)

    def password_needed(self, ssid):
        """Best-effort guess whether ssid wants a passphrase. Default: yes,
        except strip setup APs are always WPA2 with a known passphrase."""
        return None


def _run(argv, timeout=20):
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise WifiError("%s: %s" % (" ".join(argv[:2]), exc))
    return proc


class MacWifi(WifiBackend):
    name = "macOS"
    _AIRPORT = ("/System/Library/PrivateFrameworks/Apple80211.framework/"
                "Resources/airport")

    def _port(self):
        """(service, device) of the Wi-Fi hardware port, e.g. ("Wi-Fi", "en0").

        The service may be renamed by the user, so read it instead of assuming
        "Wi-Fi": passing a wrong name to -setairportnetwork fails with the
        confusing "not a Wi-Fi interface" error.
        """
        try:
            out = _run(("networksetup", "-listallhardwareports")).stdout
        except WifiError:
            return "Wi-Fi", "en0"
        pairs = []
        name = None
        for line in out.splitlines():
            if line.startswith("Hardware Port:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("Device:") and name:
                pairs.append((name, line.split(":", 1)[1].strip()))
                name = None
        for svc, dev in pairs:
            if svc.lower().replace("-", "") in ("wifi", "airport"):
                return svc, dev
        return "Wi-Fi", "en0"

    def scan(self):
        if not os.path.exists(self._AIRPORT):
            raise WifiError("airport(1) is gone on this macOS, so this script "
                            "cannot scan for the strip. Find TONLY_TAP_xxxx on "
                            "the strip's label (or your Wi-Fi menu) and pass "
                            "--ap TONLY_TAP_xxxx explicitly.")
        out = _run((self._AIRPORT, "-s")).stdout
        ssids = []
        for line in out.splitlines()[1:]:
            name = line[:32].strip()
            if name and name not in ssids:
                ssids.append(name)
        return ssids

    def current(self):
        _, dev = self._port()
        out = _run(("networksetup", "-getairportnetwork", dev)).stdout.strip()
        m = re.match(r"Current Wi-Fi Network:\s*(.+)", out)
        if m:
            return m.group(1).strip()
        return None

    def connect(self, ssid, password=None):
        _svc, dev = self._port()
        # A powered-off radio fails the join with "not a Wi-Fi interface",
        # which reads like a wrong name. Power on first; harmless if already on.
        _run(("networksetup", "-setairportpower", dev, "on"), timeout=15)
        # NOTE: despite the man page saying "service", current macOS wants the
        # DEVICE here (en0). With the service name it always answers "Wi-Fi is
        # not a Wi-Fi interface" - verified 2026-10-09 on macOS 27.
        argv = ["networksetup", "-setairportnetwork", dev, ssid]
        if password:
            argv.append(password)
        # networksetup joins from a cached scan list, so a network that only
        # just appeared ("Could not find network") can succeed seconds later
        # once the cache refreshes - the Wi-Fi menu sees it first because the
        # GUI scans continuously and the CLI does not. Retry before giving up.
        last_out, last_rc = "", 0
        for attempt in (1, 2, 3):
            proc = _run(argv, timeout=30)
            last_out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
            last_rc = proc.returncode
            # networksetup can exit 0 while printing failure ("Could not find
            # network ..."), so judge by the text, not just the return code.
            if last_rc == 0 and "could not" not in last_out.lower() \
                    and "** error" not in last_out:
                break
            if "could not find network" in last_out.lower() and attempt < 3:
                print("  join attempt %d: network not in scan cache yet, "
                      "waiting 8s ..." % attempt)
                time.sleep(8)
        else:
            pass
        out, proc_returncode = last_out, last_rc
        if proc_returncode != 0 or "could not" in out.lower() or "** error" in out:
            err = out
            if "could not find network" in err.lower():
                raise WifiError("the Mac's Wi-Fi still cannot see %r after 3 tries "
                                "(yet the Wi-Fi menu may show it - the menu scans "
                                "continuously, the CLI cache lags). Workaround that "
                                "always works: join it once from the Wi-Fi menu by "
                                "hand, then re-run this script - it detects it is "
                                "already on the setup AP and continues." % ssid)
            if "not associated" in err or "could not" in err.lower():
                hint = (" (tip: macOS may ask for an admin password to change "
                        "Wi-Fi; re-run with sudo)")
            else:
                hint = ""
            raise WifiError("could not join %r: %s%s" % (ssid, err or "unknown error", hint))
        time.sleep(3)  # DHCP + route settle before the first socket


class LinuxWifi(WifiBackend):
    name = "Linux"

    def _have_nmcli(self):
        if shutil.which("nmcli") is None:
            raise WifiError("nmcli (NetworkManager) is not installed, so this "
                            "script cannot drive Wi-Fi here. Install "
                            "NetworkManager or join the strip's setup AP by "
                            "hand and pass --ap.")
        return True

    def scan(self):
        self._have_nmcli()
        out = _run(("nmcli", "-t", "-f", "SSID", "dev", "wifi", "list",
                    "--rescan", "yes"), timeout=30).stdout
        ssids = []
        for line in out.splitlines():
            name = line.strip()
            if name and name not in ssids:
                ssids.append(name)
        return ssids

    def current(self):
        self._have_nmcli()
        out = _run(("nmcli", "-t", "-f", "NAME", "connection", "show",
                    "--active")).stdout
        for line in out.splitlines():
            name = line.strip()
            if name:
                return name  # first active connection is the route carrier
        return None

    def connect(self, ssid, password=None):
        self._have_nmcli()
        argv = ["nmcli", "dev", "wifi", "connect", ssid]
        if password:
            argv += ["password", password]
        proc = _run(argv, timeout=45)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout).strip()
            raise WifiError("could not join %r: %s" % (ssid, err or "unknown error"))
        time.sleep(2)

    def rejoin(self, name):
        self._have_nmcli()
        proc = _run(("nmcli", "connection", "up", name), timeout=45)
        if proc.returncode != 0:
            raise WifiError("could not rejoin %r: %s"
                            % (name, (proc.stderr or proc.stdout).strip()))


class WindowsWifi(WifiBackend):
    name = "Windows"

    def scan(self):
        out = _run(("netsh", "wlan", "show", "networks", "mode=bssid"),
                   timeout=30).stdout
        ssids = []
        for line in out.splitlines():
            m = re.match(r"\s*SSID \d+ : (.+)", line)
            if m:
                name = m.group(1).strip()
                if name and name not in ssids:
                    ssids.append(name)
        if not ssids and "disconnected" in out.lower():
            raise WifiError("the Wi-Fi interface looks down; enable Wi-Fi and retry.")
        return ssids

    def current(self):
        out = _run(("netsh", "wlan", "show", "interfaces")).stdout
        m = re.search(r"^\s*SSID\s*:\s*(.+)$", out, re.M)
        return m.group(1).strip() if m else None

    def _interface(self):
        out = _run(("netsh", "wlan", "show", "interfaces")).stdout
        m = re.search(r"^\s*Name\s*:\s*(.+)$", out, re.M)
        return m.group(1).strip() if m else "Wi-Fi"

    def connect(self, ssid, password=None):
        # netsh cannot join a WPA2 network with an inline passphrase; it needs
        # a stored profile. Write a minimal one to a temp file, join, delete it.
        profile = None
        try:
            if password:
                xml = (
                    '<?xml version="1.0"?>\n<WLANProfile '
                    'xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">\n'
                    "  <name>{s}</name>\n  <SSIDConfig><SSID><name>{s}</name>"
                    "</SSID></SSIDConfig>\n"
                    "  <connectionType>ESS</connectionType>\n"
                    "  <connectionMode>manual</connectionMode>\n"
                    "  <MSM><security><authEncryption>"
                    "<authentication>WPA2PSK</authentication>"
                    "<encryption>AES</encryption><useOneX>false</useOneX>"
                    "</authEncryption><sharedKey><keyType>passPhrase</keyType>"
                    "<protected>false</protected><keyMaterial>{p}</keyMaterial>"
                    "</sharedKey></security></MSM>\n</WLANProfile>\n"
                ).format(s=_xml_escape(ssid), p=_xml_escape(password))
                with tempfile.NamedTemporaryFile("w", suffix=".xml",
                                                 delete=False,
                                                 encoding="utf-8") as fh:
                    fh.write(xml)
                    profile = fh.name
                add = _run(("netsh", "wlan", "add", "profile",
                            'filename="%s"' % profile, "user=current"))
                if add.returncode != 0:
                    raise WifiError("could not store a profile for %r: %s"
                                    % (ssid, (add.stderr or add.stdout).strip()))
            proc = _run(("netsh", "wlan", "connect",
                         "name=%s" % ssid, "ssid=%s" % ssid,
                         "interface=%s" % self._interface()), timeout=45)
            if proc.returncode != 0:
                raise WifiError("could not join %r: %s"
                                % (ssid, (proc.stderr or proc.stdout).strip()))
        finally:
            if profile:
                try:
                    os.unlink(profile)
                except OSError:
                    pass
                _run(("netsh", "wlan", "delete", "profile",
                      "name=%s" % ssid))
        time.sleep(3)


def _xml_escape(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                 .replace(">", "&gt;").replace('"', "&quot;"))


def wifi_backend():
    """Pick the backend for this OS. Unknown OS -> instructions, not a crash."""
    plat = sys.platform
    if plat == "darwin":
        return MacWifi()
    if plat.startswith("linux"):
        return LinuxWifi()
    if plat in ("win32", "cygwin"):
        return WindowsWifi()
    raise WifiError("unsupported platform %r: join the strip's setup AP by hand "
                    "and pass --ap explicitly" % plat)


# ------------------------------------------------------------- network helpers

def default_gateway():
    """Our default gateway on the interface currently carrying the route.

    Linux exposes it via `ip`; macOS (and BSD) do not ship `ip` at all, so fall
    back to `route -n get default`, and Windows reads it from ipconfig. While
    joined to the strip's setup AP this IS the strip's own IP, which is why the
    gateway doubles as the --gateway default.
    """
    if sys.platform in ("win32", "cygwin"):
        try:
            out = subprocess.run(("ipconfig",), capture_output=True, text=True,
                                 timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        gateways = re.findall(r"Default Gateway[ .]*:\s*(\d+\.\d+\.\d+\.\d+)", out)
        return gateways[-1] if gateways else None
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


def guess_controller_ip():
    """Best-effort default for the controller prompt: homeassistant.local.

    The strip must dial the Home Assistant host's LAN address, which no scan
    from here can know for sure - but a stock HA install answers mDNS at
    homeassistant.local, so try that before asking the user to type an address.
    """
    try:
        return socket.gethostbyname("homeassistant.local")
    except OSError:
        return None


# ------------------------------------------------------------- strip protocol

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


def provision_over_socket(gw, controller, ssid, password, reboot=False):
    """The three-line setup dialect. Raises RuntimeError / exits on failure."""
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
        command(s, "up:ip:%s" % controller, expect="ip_ok",
                what="controller callback address")
        time.sleep(0.4)
        command(s, "up:connect:%s:%s" % (ssid, password), expect="connect_ok",
                what="Wi-Fi credentials")
        if reboot:
            print("  (sending optional reboot command)")
            command(s, "up:reboot:0", expect_ack=False, timeout=2.0)
    except RuntimeError as e:
        sys.exit("error: %s" % e)
    finally:
        try:
            s.close()
        except OSError:
            pass


def verify_at_controller(controller):
    """Best-effort check that the controller is reachable and listing devices.

    From a laptop on the LAN, port 8099 is EXPECTED to refuse: the add-on only
    publishes it on its internal network (no authentication, so it must never
    face the LAN). A refusal therefore means nothing; run the same curl from
    the HA host (or the SSH add-on) instead. Only a wrong-controller typo is
    worth surfacing, and even that cannot be told apart from the LAN from here.
    """
    import json
    import urllib.request
    url = "http://%s:8099/api/devices" % controller
    try:
        with urllib.request.urlopen(url, timeout=6) as resp:
            body = json.loads(resp.read().decode())
    except Exception as exc:
        print("  controller check skipped (%s)" % exc)
        print("  From the HA host itself, watch for the strip with:")
        print("    curl -s http://%s:8099/api/devices | python3 -m json.tool"
              % controller)
        return
    devs = body.get("devices") or []
    print("  controller at %s lists %d device(s): %s"
          % (controller, len(devs),
             ", ".join(str(d.get("devid")) for d in devs) or "none yet"))


# ------------------------------------------------------------------ main flow

def pick_setup_ap(backend, explicit):
    """Return the strip's setup SSID, via --ap, a scan, or a typed name."""
    if explicit:
        if not strip_is_setup_ap(explicit):
            print("  WARNING: %r does not look like a strip setup AP "
                  "(expected TONLY_TAP_xxxx); trying it anyway." % explicit)
        return explicit.strip()
    print("scanning for the strip's setup network ...")
    try:
        found = [s for s in backend.scan() if strip_is_setup_ap(s)]
    except WifiError as exc:
        print("  %s" % exc)
        found = []
    if len(found) == 1:
        print("  found setup AP: %s" % found[0])
        return found[0]
    if len(found) > 1:
        print("  several setup APs are visible:")
        for i, name in enumerate(found, 1):
            print("    %d) %s" % (i, name))
        while True:
            choice = input("  provision which one [1-%d]? " % len(found)).strip()
            if choice.isdigit() and 1 <= int(choice) <= len(found):
                return found[int(choice) - 1]
    print("  no setup AP found. The strip broadcasts TONLY_TAP_xxxx while it "
          "blinks in pairing mode; phones hide it unless Location is on.")
    while True:
        name = input("  setup AP name (or empty to quit)? ").strip()
        if not name:
            sys.exit("aborted: no setup AP selected")
        if name.upper().startswith("LGU_"):
            # The single most common typo on this prompt: the PASSPHRASE goes
            # in the Wi-Fi password field later - here we need the NETWORK name
            # (TONLY_TAP_xxxx). Catch it instead of joining garbage.
            print("  that is the passphrase, not the network name: the setup AP "
                  "is called TONLY_TAP_xxxx (see the strip's label). Try again.")
            continue
        if not strip_is_setup_ap(name):
            print("  WARNING: %r does not look like a strip setup AP "
                  "(expected TONLY_TAP_xxxx); trying it anyway." % name)
        return name


def main():
    ap = argparse.ArgumentParser(
        description="Join a TONLY/MTTL-W01 strip to your Wi-Fi and point it at "
                    "your controller. Remembers home Wi-Fi + controller in "
                    "%s." % config_path())
    ap.add_argument("--ssid", default="", help="home Wi-Fi name")
    ap.add_argument("--password", default="", help="home Wi-Fi password")
    ap.add_argument("--ap", default="", help="setup SSID, e.g. TONLY_TAP_3355BA3")
    ap.add_argument("--gateway", default="", help="strip IP (setup AP gateway)")
    ap.add_argument("--controller", default="",
                    help="Home Assistant host LAN IP the strip should call back")
    ap.add_argument("--config", default="",
                    help="settings file (default %s)" % config_path())
    ap.add_argument("--save", action="store_true",
                    help="remember home Wi-Fi + controller IP in the settings file")
    ap.add_argument("--no-save", dest="save", action="store_false",
                    help="do not touch the settings file")
    ap.set_defaults(save=None)
    ap.add_argument("--rejoin", dest="rejoin", action="store_true",
                    help="rejoin the previous network afterwards (default)")
    ap.add_argument("--no-rejoin", dest="rejoin", action="store_false",
                    help="stay on the strip's setup AP afterwards")
    ap.set_defaults(rejoin=True)
    ap.add_argument("--verify", dest="verify", action="store_true",
                    help="ask the controller whether it sees devices (default)")
    ap.add_argument("--no-verify", dest="verify", action="store_false")
    ap.set_defaults(verify=True)
    ap.add_argument("--reboot", action="store_true",
                    help="also send up:reboot:0 (optional; the strip normally "
                         "reboots itself and the proven flow omits it)")
    a = ap.parse_args()

    cfg_path = config_path(a.config or None)
    saved = load_settings(cfg_path)

    # Flags beat the file; the file beats an interactive prompt.
    ssid = (a.ssid.strip() or saved.get("HOME_SSID", "").strip())
    password = a.password or saved.get("HOME_PASSWORD", "")
    controller = (a.controller.strip() or saved.get("CONTROLLER_IP", "").strip())
    if not ssid:
        ssid = input("home Wi-Fi name (SSID)? ").strip()
        if not ssid:
            sys.exit("aborted: no home Wi-Fi given")
    ssid = _normalize_quotes(ssid, "SSID")
    _reject_bad_chars(ssid, "SSID")
    if not password:
        password = getpass.getpass("password for %r (hidden)? " % ssid)
        if not password:
            sys.exit("aborted: no Wi-Fi password given")
    password = _normalize_quotes(password, "Wi-Fi password")
    _reject_bad_chars(password, "Wi-Fi password")
    if not controller:
        guess = guess_controller_ip()
        hint = " [%s]" % guess if guess else ""
        controller = input("controller IP (Home Assistant host LAN)%s? " % hint).strip() or (guess or "")
        if not controller:
            sys.exit("aborted: no controller IP given "
                     "(the strip dials this on TCP %d)" % CALLBACK_PORT)

    if a.save or (a.save is None and (not saved.get("HOME_SSID")
                                      or not saved.get("CONTROLLER_IP"))):
        save_settings(cfg_path, {"HOME_SSID": ssid,
                                 "HOME_PASSWORD": password,
                                 "CONTROLLER_IP": controller})
        print("  remembered in %s (mode 600)" % cfg_path)

    try:
        backend = wifi_backend()
    except WifiError as exc:
        sys.exit("error: %s" % exc)

    try:
        previous = backend.current()
    except WifiError:
        previous = None
    if previous:
        print("currently on Wi-Fi : %s (will rejoin afterwards)" % previous)

    setup_ap = pick_setup_ap(backend, a.ap)
    setup_pw = passphrase_for(setup_ap)
    if not setup_pw:
        sys.exit("error: cannot derive the setup passphrase from %r "
                 "(expected TONLY_TAP_<suffix>)" % setup_ap)

    print()
    print("target Wi-Fi      : %s" % ssid)
    print("controller IP     : %s   (strip will dial this on TCP %d)"
          % (controller, CALLBACK_PORT))
    print("setup AP          : %s   passphrase %s" % (setup_ap, setup_pw))

    if previous != setup_ap:
        print("joining setup AP %s ..." % setup_ap)
        try:
            backend.connect(setup_ap, setup_pw)
        except WifiError as exc:
            sys.exit("error: %s\n"
                     "  - confirm the strip is powered and blinking\n"
                     "  - on macOS, joining Wi-Fi from the terminal can need "
                     "sudo; on Windows, the Wi-Fi switch must be on" % exc)
    else:
        print("already on the setup AP.")

    gw = a.gateway.strip() or default_gateway()
    print("strip IP          : %s:%d" % (gw or "<autodetect>", SETUP_PORT))
    print()
    if not gw:
        sys.exit("error: cannot detect the setup AP gateway; pass --gateway "
                 "(usually the AP's own address)")

    provision_over_socket(gw, controller, ssid, password, reboot=a.reboot)

    print()
    print("Provisioning accepted by the strip (ip_ok + connect_ok).")
    print("The strip is rebooting itself onto '%s'." % ssid)

    if a.rejoin:
        home = previous if previous and previous != setup_ap else ssid
        print("rejoining %s ..." % home)
        try:
            if isinstance(backend, LinuxWifi):
                backend.rejoin(home)
            else:
                backend.connect(home, password if home == ssid else None)
            print("  back on %s." % home)
        except WifiError as exc:
            print("  WARNING: automatic rejoin failed (%s)." % exc)
            print("  Rejoin '%s' by hand; the strip side is already done." % home)

    if a.verify:
        print("asking the controller whether it is reachable ...")
        verify_at_controller(controller)
    print("Done. The strip should call %s:%d on its own within a minute."
          % (controller, CALLBACK_PORT))


if __name__ == "__main__":
    main()
