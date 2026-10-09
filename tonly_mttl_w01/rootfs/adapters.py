"""
Protocol adapters for the TONLY MTTL-W01 controller (TONLY / MTTL-W01 strip).

The wire protocol below was recovered from the vendor's own app
(FG Link, com.fgmachines.rck 1.6.13) by decompiling its dex. It is a plain
text, CRLF-delimited, line oriented protocol.

Topology (this is the part that matters)
----------------------------------------
The strip is NOT a TCP server we connect to. Once provisioned it dials
*us*. Provisioning tells it which address to dial.

  Provisioning (strip sitting in its setup access point)
      client -> TCP <setup-AP-gateway>:30300
      client -> "up:ip:<controller-lan-ip>\r\n"        (acked)
      client -> "up:connect:<ssid>:<password>\r\n"      (acked)
      client -> "up:reboot:0\r\n"                       (no ack)
      The strip reboots, joins <ssid>, and connects back to the address
      given in "up:ip:".

  Normal operation
      strip -> TCP <controller>:10086
      strip -> "up:bootinfo:lgutap;<devid>;<devid>;<fw>;connect"

      The strip will not send that hello unprompted. Measured on the wire it
      opens the socket, emits a single byte, then stalls. The controller has to
      send "up:getinfo:all" straight away, and the hello arrives only after.

      controller -> "up:getinfo:all\\r\\n"   (immediately, then every 10 s)
      controller -> "up:onoff:<1-4>:on|off\\r\\n"
      strip -> "up:getinfo:1:<12 fields>:2:<...>:3:<...>:4:<...>"
      strip -> "up:event:onoff:<1-4>:on|off\\r\\n"   (spontaneous change)

Note the setup dialect is text and colon separated, so the target SSID and
password must not contain ':'.
"""
import json
import os
import re
import socket
import socketserver
import sys
import threading
import time

DEFAULT_HOST = "10.77.0.2"
DEFAULT_PORT = 30300
CHANNELS = 4

# --- protocol constants (decoded from the vendor app) --------------------
CMD_GETINFO_ALL = "up:getinfo:all"
CMD_ONOFF = "up:onoff:%d:%s"
CMD_REBOOT = "up:reboot:0"
# Read-only queries. The strip answers with data and switches nothing, so these
# are safe against a live strip and against a protected socket. The firmware has
# no voltage FIELD in the 12-field status block, but it does answer an active
# query with millivolts (>= 50000) or per-outlet milliamps (< 50000).
CMD_POWER_REPORT_VOL = "up:power_report:1:vol"
CMD_QUERY_RSSI = "up:query:wifirssi"
DEVICE_PORT = 10086
SETUP_PORT = 30300
MODEL = "lgutap"

# The hello the strip sends on connect. Measured from the physical device:
#   up:bootinfo:lgutap;2CFDB3355BA3;2cfdb3355ba3;0.1.54-1.0.66;connect
# The vendor app also handled an obfuscated spelling (b\x0Fotinfo) with a
# 12 hex device id in both slots, so accept either. Note the real device
# differs in case between the two id fields.
# The hello the strip sends on connect. Measured from the physical device:
#   up:bootinfo:lgutap;2CFDB3355BA3;2cfdb3355ba3;0.1.54-1.0.66;connect
# The vendor app also handled an obfuscated spelling (b\x0Fotinfo) with a
# 12 hex device id in both slots, so accept either. Note the real device
# differs in case between the two id fields.
RE_HELLO = re.compile(
    r"^up:(?:bootinfo|otinfo|b[\x00-\x1f/]?otinfo):([^;\r\n]{1,32});"
    r"([0-9A-Fa-f]{6,16});([0-9A-Fa-f]{6,16});([^;\r\n]{1,64});connect$")

RE_EVENT = re.compile(r"^up:(?:event:)?onoff:([1-4]):(on|off)$")
RE_POWER_REPORT = re.compile(r"^up:power_report:([1-5]):(-?\d+)$", re.I)
RE_QUERY = re.compile(r"^up:query:(-?\d+)$", re.I)
RE_STATUS = re.compile(
    r"^up:getinfo:(?:(?:[1-4]):(?:[^:;]{0,64};){11}[^:;]{0,64}:){3}"
    r"[1-4]:(?:[^:;]{0,64};){11}[^:;]{0,64}$")


def _log(msg):
    """One-line trace to stderr (journald on the LXC), on by default.

    The link to a physical strip is invisible from HTTP responses alone, and
    this is the only record of what the firmware actually says on the wire.
    """
    print("[mttl] %s" % msg, file=sys.stderr, flush=True)


class AdapterError(Exception):
    pass


class Adapter:
    name = "base"

    def probe(self):
        raise NotImplementedError

    def measure(self, devid=None, timeout=3.0):
        """Ask the strip for a voltage/current/RSSI reading.

        Only the callback (MTTL) adapter can do this, because it is the only one
        holding the socket the strip called out on. Adapters that cannot answer
        must raise rather than return a number, so a missing measurement is
        never mistaken for a real one.
        """
        raise AdapterError(
            "%s cannot measure: voltage is only available on the callback "
            "connection to a real strip" % self.name)

    def get_state(self, devid=None):
        raise NotImplementedError

    def set_switch(self, idx, on, devid=None):
        raise NotImplementedError

    def is_simulated(self):
        return True

    def list_devices(self):
        """Strips this adapter can talk to. Single-device adapters report one."""
        raise NotImplementedError

    def current_device(self, devid=None):
        """The device id a state read actually came from.

        Callers must ask this rather than assume: when several strips are
        connected the default is the most recent one, so reporting anything else
        would be a lie the user cannot detect from the response.
        """
        raise NotImplementedError

    def describe(self):
        return {"adapter": self.name}


def _norm_devid(s):
    return (s or "").strip().upper()


def _clean(s):
    """Strip the line padding the real firmware adds.

    Measured from the strip: every line arrives padded out to a fixed size
    with NUL bytes, e.g. b"\\x00\\x00...up:getinfo:1:0;off;...". str.strip()
    does not remove NUL, so these have to go explicitly or nothing parses.
    """
    return (s or "").strip("\x00 \t\r\n\x0b\x0c")


# The vendor app divides the power field by 1000 to get watts and the energy
# field by 1000 to get kWh (f/AbstractC0187Hf.java: dIntValue = f(5)/1000.0,
# dLongValue = d(8,6)/1000.0). We could not reproduce that scale:
#   - a load labelled 60 W read raw 16745, which is 16.75 W at /1000
#   - the server outlet reads ~12000-18000 raw, i.e. ~12-18 W, which is far too
#     low for a Proxmox host running VMs
# The field does respond to real load (it rose 12525 -> 16765 as a load settled,
# and drops to 0 when the relay opens), so it is measuring something real. The
# divisor is simply unconfirmed. Every converted value is therefore returned
# with "verified": False and the raw number alongside it, so the UI can be
# honest and nothing is silently wrong.
POWER_DIVISOR = 1000.0
ENERGY_DIVISOR = 1000.0


def _num(v, default=None):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _telemetry_from(fields):
    """Pull the measured fields out of one outlet's 12-field block."""
    power_raw = _num(fields[5], 0) or 0
    energy_raw = _num(fields[6], 0) or 0
    code = fields[10].strip().upper()
    temp_raw = _num(fields[11])
    return {
        # Fields 3 and 4 read "on" on EVERY channel including one with nothing
        # plugged in, so they are NOT overload/overheat as the vendor's field
        # order suggested. An earlier version of this file reported them as
        # safety flags and produced "overload: true" on an empty socket, which
        # is how the mistake was caught. We do not know what they mean and we
        # are not going to invent an interpretation, so they are carried as
        # unlabelled raw values instead.
        "flag3": fields[3].strip(),
        "flag4": fields[4].strip(),
        # instantaneous power, vendor scale, explicitly unverified
        "power_w": round(power_raw / POWER_DIVISOR, 2),
        "power_raw": power_raw,
        # accumulating energy register
        "energy_kwh": round(energy_raw / ENERGY_DIVISOR, 3),
        "energy_raw": energy_raw,
        "temp_c": temp_raw,
        # "00" normal, "AB" seen when a relay is open with residual current
        "state_code": code,
        "power_verified": False,
        "draws_current": bool(power_raw),
    }


class MttlAdapter(Adapter):
    """Real hardware. Listens on TCP 10086 for the strip to dial in.

    Nothing here simulates anything: if no strip is connected, get_state()
    raises rather than inventing switch states.
    """

    name = "mttl"

    def __init__(self, bind="0.0.0.0", port=DEVICE_PORT, keepalive=10.0):
        self.bind = bind
        self.port = int(port)
        self.keepalive = float(keepalive)
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._devices = {}          # devid -> _Session
        # Kept only so single-strip callers that predate multi-device support keep
        # working: they mirror the currently selected device's state. The truth
        # is per-session (see _Session.switches), because more than one strip can
        # dial this listener at the same time.
        self._switches = {}         # channel -> bool (mirror of selected device)
        # channel -> telemetry dict, from the same getinfo line. Field meanings
        # come from the vendor app's own parser (f/AbstractC0187Hf.java, method
        # c), which is the only authoritative description of this protocol:
        #   1 relay   3 overload   4 overheat   5 power   6 energy   10 code 11 temp
        self._telemetry = {}
        self._seen = {}             # devid -> hello dict
        self._selected = None       # devid currently mirrored into the above
        self.last_event = 0.0
        self.listen_error = None
        self._stop = threading.Event()
        self._srv = None
        self._start()

    # -- lifecycle ------------------------------------------------------
    def _start(self):
        self._srv = _MttlServer((self.bind, self.port), self)
        t = threading.Thread(target=self._srv.serve_forever,
                             kwargs={"poll_interval": 0.5},
                             name="mttl-accept", daemon=True)
        t.start()
        ka = threading.Thread(target=self._keepalive, name="mttl-keepalive",
                              daemon=True)
        ka.start()

    def close(self):
        self._stop.set()
        try:
            self._srv.shutdown()
            self._srv.server_close()
        except Exception:
            pass

    # -- incoming -------------------------------------------------------
    def on_line(self, sess, raw):
        """Handle one CRLF line received from the strip."""
        line = _clean(raw)
        if not line:
            return
        m = RE_HELLO.match(line)
        if m:
            devid = _norm_devid(m.group(2))
            if devid != _norm_devid(m.group(3)):
                sess.close()
                return
            if m.group(1).strip().lower() != MODEL:
                # Vendor refuses anything that is not its own model.
                sess.close()
                return
            sess.devid = devid
            with self._lock:
                old = self._devices.get(devid)
                if old is not None and old is not sess:
                    old.close()
                self._devices[devid] = sess
                self._seen[devid] = {"model": m.group(1).strip(),
                                     "devid": devid,
                                     "firmware": m.group(4).strip(),
                                     "hello": line,
                                     "remote": sess.remote,
                                     "at": time.time()}
            self.last_event = time.time()
            sess.send(CMD_GETINFO_ALL)
            return

        me = RE_EVENT.match(line)
        if me:
            ch, st = int(me.group(1)), me.group(2) == "on"
            # State lives on the SESSION, not on the adapter. Two strips can dial
            # the same listener, and a single shared dict would merge their
            # channels: socket 3 of strip A reading as socket 3 of strip B is
            # exactly the kind of lie this service is not allowed to tell.
            sess.switches[ch] = st
            self.last_event = time.time()
            return

        # Replies to the read-only queries. These arrive on their own, outside
        # any status block, and were previously discarded as unrecognised.
        # Recorded raw so the interpretation can be revisited without having to
        # provoke the strip again.
        mp = RE_POWER_REPORT.match(line)
        if mp:
            sess.reports[mp.group(1).lower()] = {
                "kind": "power_report",
                "channel": int(mp.group(1)),
                "raw": int(mp.group(2)),
                "line": line,
                "at": time.time(),
            }
            self.last_event = time.time()
            return
        mq = RE_QUERY.match(line)
        if mq:
            sess.reports["wifirssi"] = {
                "kind": "query",
                "raw": int(mq.group(1)),
                "line": line,
                "at": time.time(),
            }
            self.last_event = time.time()
            return

        # status block. The firmware sends it across several TCP segments, so
        # accumulate until it parses.
        st = self._parse_status(line, want_telemetry=True)
        if st:
            switches, telemetry = st
            sess.switches.update(switches)
            sess.telemetry.update(telemetry)
            self.last_event = time.time()

    @staticmethod
    def _parse_status(text, want_telemetry=False):
        """Parse 'up:getinfo:1:<12 fields>:...:4:<12 fields>'.

        Each outlet block is 12 ';' separated fields. Field meanings are taken
        from the vendor app's own parser (f/AbstractC0187Hf.java method c),
        which divides field 5 by 1000 for watts and field 6 by 1000 for kWh.

        That divisor is the VENDOR's claim and we could not verify it: a known
        60 W load read raw 16745 (=> /1000 = 16.75 W, /279 = 60 W), and the
        server reads ~14 raw-thousandths which is implausibly low for a Proxmox
        host. So the converted values are returned but flagged unverified, and
        the raw numbers are always kept alongside so nothing is lost.
        """
        text = _clean(text)
        if not text.startswith("up:getinfo:"):
            return None
        parts = text[len("up:getinfo:"):].split(":")
        if len(parts) != 8:
            return None
        out = {}
        tele = {}
        for i in range(0, 8, 2):
            try:
                ch = int(parts[i])
            except ValueError:
                return None
            if not 1 <= ch <= CHANNELS:
                return None
            fields = parts[i + 1].split(";")
            if len(fields) != 12:
                return None
            if fields[1].strip().lower() not in ("on", "off"):
                return None
            out[ch] = fields[1].strip().lower() == "on"
            if want_telemetry:
                tele[ch] = _telemetry_from(fields)
        if len(out) != CHANNELS:
            return None
        return (out, tele) if want_telemetry else out

    # -- outgoing -------------------------------------------------------
    def _keepalive(self):
        while not self._stop.wait(self.keepalive):
            with self._lock:
                sessions = list(self._devices.values())
            for s in sessions:
                s.send(CMD_GETINFO_ALL)

    def send(self, cmd, devid=None):
        """Send to one device, or to every connected device when devid is None."""
        with self._lock:
            sessions = list(self._devices.values())
        if devid:
            want = _norm_devid(devid)
            sessions = [s for s in sessions if _norm_devid(s.devid or "") == want]
            if not sessions:
                raise AdapterError("no strip with device id %s is connected" % want)
        if not sessions:
            raise AdapterError("no MTTL strip is connected (waiting on TCP %d)"
                               % self.port)
        last = None
        for s in sessions:
            try:
                s.send(cmd)
                last = s
            except Exception as exc:
                last = exc
        if isinstance(last, Exception):
            raise AdapterError(str(last))
        return True

    # -- Adapter API ---------------------------------------------------
    def measure(self, devid=None, timeout=3.0):
        """Ask the strip the read-only measurement queries and collect the replies.

        Named measure(), NOT probe(): probe() already exists on this class and
        means 'describe the listener', which /api/health calls. Shadowing it
        would quietly change what the health endpoint reports.

        Sends only CMD_POWER_REPORT_VOL and CMD_QUERY_RSSI. Neither touches a
        relay, so this is safe to run against a strip with a protected socket
        and cannot change the state of any outlet.

        MEASURED on Karim's strip (2026-10-04): every channel 1..4 answers with
        the SAME mains voltage in millivolts (207.7-208.0 V across channels,
        0.15% spread = noise), and channel 5 answers 0.

        So the strip exposes a VOLTAGE and nothing else. There is NO per-outlet
        current: the 'a value below 50000 is per-outlet milliamps' rule comes
        from the powerk project's fixture, and no real strip has been seen to
        produce one. That is why no current is reported here even when a channel
        answers 0 - a zero is 'nothing to report', not a measured 0 A.
        """
        target = devid
        if target is None:
            sess = self.resolve(None)
            if sess is not None:
                target = sess.devid
        for s in ([x for x in (self.resolve(target),) if x is not None]):
            s.reports.clear()
        # Channel 1 answers in millivolts. Sweeping 1..5 asks for the other
        # channels too: a reply under 50000 is that channel's current in
        # milliamps, which is the only route to a real watt figure.
        for ch in range(1, CHANNELS + 2):
            self.send("up:power_report:%d:vol" % ch, devid=target)
        self.send(CMD_QUERY_RSSI, devid=target)

        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                sessions = list(self._devices.values())
            got = []
            for s in sessions:
                if target and _norm_devid(s.devid or "") != _norm_devid(target):
                    continue
                got.extend(s.reports.values())
            # Wait until every channel has answered or the window closes, so a
            # partial sweep is visible as partial rather than as a whole answer.
            answered = {r["channel"] for r in got if r["kind"] == "power_report"}
            if len(answered) >= CHANNELS + 1 and \
               any(r["kind"] == "query" for r in got):
                break
            time.sleep(0.1)

        volts = None
        volts_by_channel = {}
        milliamps = {}
        rssi = None
        for r in got:
            if r["kind"] == "power_report":
                if r["raw"] >= 50000:
                    # Every channel answers with the same mains voltage, so the
                    # median is used rather than whichever reply landed last.
                    volts_by_channel[r["channel"]] = round(r["raw"] / 1000.0, 1)
                elif 0 < r["raw"] < 50000 and r["channel"] <= CHANNELS:
                    # Never seen on real hardware. Recorded if it ever appears,
                    # but never invented from a zero.
                    milliamps[r["channel"]] = round(r["raw"] / 1000.0, 3)
            elif r["kind"] == "query":
                rssi = r["raw"]
        if volts_by_channel:
            vals = sorted(volts_by_channel.values())
            volts = vals[len(vals) // 2]
        return {
            "device": target,
            "voltage_v": volts,
            "voltage_channels": volts_by_channel,
            "voltage_spread_v": (round(max(volts_by_channel.values())
                                      - min(volts_by_channel.values()), 2)
                                 if len(volts_by_channel) > 1 else 0.0),
            "outlet_ma": milliamps,
            "current_available": bool(milliamps),
            "rssi_dbm": rssi,
            "note": None if milliamps else
                    "strip reports mains voltage only; it exposes no per-outlet "
                    "current, so watts cannot be converted to amps",
            "raw": got,
        }

    def list_devices(self):
        """Every strip that has ever said hello, with a connected flag."""
        with self._lock:
            live = set(self._devices)
            seen = dict(self._seen)
        out = []
        for devid, info in seen.items():
            d = dict(info)
            d["connected"] = devid in live
            out.append(d)
        out.sort(key=lambda d: not d["connected"])
        return out

    def resolve(self, devid=None):
        """Which device a bare /api/state request means.

        An explicit request always wins. With none, the FIRST strip that said
        hello is used, and it keeps being used.

        That is deliberate. "Most recently connected" sounds friendlier but is
        worse in practice: the moment a second strip is plugged in it would
        become the default, and every existing caller that does not name a
        device - Home Assistant above all - would silently start controlling a
        different strip. Stability beats novelty here. Callers that care pass
        device=, and /api/state reports "ambiguous" so they can be told.
        """
        with self._lock:
            sessions = [s for s in self._devices.values() if s.devid]
            if not sessions:
                return None
            if devid:
                want = _norm_devid(devid)
                for s in sessions:
                    if _norm_devid(s.devid) == want:
                        return s
                raise AdapterError(
                    "no strip with device id %s is connected; connected: %s"
                    % (want, ", ".join(sorted(str(s.devid) for s in sessions))))
            return sessions[0]

    def probe(self):
        with self._lock:
            n = len(self._devices)
            seen = dict(self._seen)
        return {"ok": n > 0 and self.listen_error is None,
                "simulated": False,
                "listen": "%s:%d" % (self.bind, self.port),
                "listening": self.listen_error is None,
                "listen_error": self.listen_error,
                "connected": n,
                "devices": seen}

    def current_device(self, devid=None):
        sess = self.resolve(devid)
        return sess.devid if sess is not None else None

    def get_state(self, devid=None):
        sess = self.resolve(devid)
        if sess is None:
            raise AdapterError(
                "no MTTL strip is connected; waiting on TCP %d for it to "
                "call back" % self.port)
        with self._state_lock:
            self._selected = sess.devid
            sw = dict(sess.switches)
            tele = dict(sess.telemetry)
        if len(sw) < CHANNELS:
            missing = sorted(set(range(1, CHANNELS + 1)) - set(sw))
            raise AdapterError(
                "strip %s has not reported channels %s yet"
                % (sess.devid, missing))
        empty = _telemetry_from(["0", "off"] + ["0"] * 10)
        return [{"id": i, "on": bool(sw.get(i, False)), **tele.get(i, empty)}
                for i in range(1, CHANNELS + 1)]

    def set_switch(self, idx, on, devid=None):
        idx = int(idx)
        if not 1 <= idx <= CHANNELS:
            raise AdapterError("outlet must be 1..%d" % CHANNELS)
        # Target the strip the state came from, so a command can never land on a
        # different strip than the one the user was looking at.
        target = devid
        if target is None:
            sess = self.resolve(None)
            if sess is not None:
                target = sess.devid
        self.send(CMD_ONOFF % (idx, "on" if on else "off"), devid=target)
        # Ask for a fresh status straight away. The relay state is echoed
        # immediately by the firmware, but the current reading only changes in
        # the next status block - without this prompt the app would show a
        # socket that is now OFF next to a stale non-zero power figure until the
        # next keepalive, up to ten seconds later.
        try:
            self.send(CMD_GETINFO_ALL, devid=target)
        except AdapterError:
            pass
        with self._state_lock:
            if target and _norm_devid(self._selected or "") == _norm_devid(target):
                self._switches[idx] = bool(on)
        return {"id": idx, "on": bool(on), "device": target}

    def describe(self):
        return {"adapter": self.name, "listen": "%s:%d" % (self.bind, self.port),
                "devices": dict(self._seen)}

    def is_simulated(self):
        return False


class _Session:
    """One accepted TCP connection from a strip."""

    def __init__(self, sock, adapter):
        self.sock = sock
        self.a = adapter
        self.devid = None
        self.remote = "%s:%s" % sock.getpeername()[:2]
        # Per-connection state. Several strips can dial the listener at once, so
        # this cannot live on the adapter. Written from this session's reader
        # thread and read from the HTTP threads, so treat it as a snapshot.
        self.switches = {}         # channel -> bool
        self.telemetry = {}        # channel -> telemetry dict
        self.reports = {}          # replies to read-only queries, raw
        self.at = time.time()
        self._lock = threading.Lock()
        self._buf = ""
        self._closed = False

    def send(self, cmd):
        with self._lock:
            if self._closed:
                raise AdapterError("connection closed")
            self.sock.sendall((cmd + "\r\n").encode())

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self.sock.close()
        except OSError:
            pass

    def serve(self):
        buf = ""
        try:
            with self.sock.makefile("rb") as fh:
                while not self._closed:
                    chunk = fh.readline()
                    if not chunk:
                        break
                    if len(chunk) > 65536:
                        continue
                    line = _clean(chunk.decode("utf-8", "replace"))
                    if not line:
                        continue
                    _log("recv %s: %r" % (self.remote, line))
                    self.a.on_line(self, line)
        except OSError:
            pass
        finally:
            _log("closed %s (devid=%s)" % (self.remote, self.devid))
            self.close()
            if self.devid:
                with self.a._lock:
                    if self.a._devices.get(self.devid) is self:
                        del self.a._devices[self.devid]


class _MttlServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, adapter):
        self.adapter = adapter
        super().__init__(addr, _MttlHandler)

    def handle_error(self, request, client_address):
        pass


class _MttlHandler(socketserver.BaseRequestHandler):
    def handle(self):
        sess = _Session(self.request, self.server.adapter)
        # The real strip opens the connection and then waits: measured on the
        # wire it sends a single byte and stalls until it is spoken to. So we
        # prompt it immediately instead of waiting for its hello, which never
        # arrives unprompted.
        sess.send(CMD_GETINFO_ALL)
        _log("accepted %s, prompted with %r" % (sess.remote, CMD_GETINFO_ALL))
        sess.serve()


class TcpAdapter(Adapter):
    """Legacy guess-based adapter, kept only so the old 'tcp' mode still runs.

    It connected *out* to the device, which is the wrong way round for this
    hardware; see MttlAdapter.
    """

    name = "tcp"

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, timeout=5.0):
        self.host = host
        self.port = int(port)
        self.timeout = timeout
        self._lock = threading.Lock()
        self.hello = None

    def _session(self, commands):
        out = []
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as s:
                s.settimeout(self.timeout)
                fh = s.makefile("rwb")
                banner = fh.readline()
                if not banner:
                    raise AdapterError("device closed the connection immediately")
                try:
                    self.hello = json.loads(banner.decode().strip())
                except Exception:
                    self.hello = {"raw": banner.decode(errors="replace").strip()}
                out.append(self.hello)
                for cmd in commands:
                    fh.write((json.dumps(cmd) + "\n").encode())
                    fh.flush()
                    reply = fh.readline()
                    if not reply:
                        raise AdapterError("device stopped responding")
                    try:
                        out.append(json.loads(reply.decode().strip()))
                    except Exception:
                        out.append({"raw": reply.decode(errors="replace").strip()})
        except socket.timeout:
            raise AdapterError("timeout talking to %s:%d" % (self.host, self.port))
        except OSError as exc:
            raise AdapterError("cannot reach %s:%d (%s)" % (self.host, self.port, exc))
        return out

    def probe(self):
        try:
            replies = self._session([{"cmd": "ping"}])
            hello = replies[0]
            simulated = bool(hello.get("sim")) if isinstance(hello, dict) else False
            return {"ok": True, "host": self.host, "port": self.port,
                    "simulated": simulated,
                    "hello": hello, "reply": replies[-1]}
        except AdapterError as exc:
            return {"ok": False, "host": self.host, "port": self.port, "error": str(exc)}

    def get_state(self, devid=None):
        with self._lock:
            replies = self._session([{"cmd": "get"}])
        state = replies[-1]
        sw = state.get("switches") or []
        return [{"id": int(s.get("id", i + 1)), "on": bool(s.get("on"))}
                for i, s in enumerate(sw)]

    def list_devices(self):
        return [{"devid": "TCP", "model": "outbound tcp probe", "connected": False,
                 "remote": "%s:%d" % (self.host, self.port), "at": time.time()}]

    def current_device(self, devid=None):
        return "TCP"

    def set_switch(self, idx, on, devid=None):
        idx = int(idx)
        on = bool(on)
        if not 1 <= idx <= CHANNELS:
            raise AdapterError("switch id must be 1..%d" % CHANNELS)
        with self._lock:
            replies = self._session([{"cmd": "set", "id": idx, "on": on}])
        ack = replies[-1]
        if ack.get("type") == "error":
            raise AdapterError(ack.get("msg", "device rejected the command"))
        return {"id": idx, "on": ack.get("on", on)}

    def describe(self):
        return {"adapter": self.name, "host": self.host, "port": self.port,
                "hello": self.hello}


class SimAdapter(Adapter):
    """In-process simulation, so the app, the HA integration and the phone UI
    are all testable while there is no strip on the network."""

    name = "sim"

    def __init__(self, path=None):
        self.path = path or os.environ.get(
            "TONLY_MTTL_W01_SIM_STATE", "/opt/tonly-mttl-w01/sim-state.json")
        self._lock = threading.Lock()

    def _load(self):
        try:
            with open(self.path) as fh:
                d = json.load(fh)
            return {int(k): bool(v) for k, v in d.items()}
        except Exception:
            return {i: False for i in range(1, CHANNELS + 1)}

    def _save(self, st):
        try:
            with open(self.path, "w") as fh:
                json.dump({str(k): v for k, v in st.items()}, fh)
        except Exception:
            pass

    def probe(self):
        return {"ok": True, "simulated": True,
                "hello": {"type": "hello", "model": "MTTL-W01(sim)", "channels": CHANNELS}}

    def measure(self, devid=None, timeout=3.0):
        """A simulator has no mains to measure.

        Returns nulls and says so, rather than a plausible-looking number. A
        faked voltage here would be indistinguishable in the app from a real
        reading off real hardware, which is the one thing this service must
        never do.
        """
        self._check_devid(devid)
        return {"device": "SIM", "simulated": True, "voltage_v": None,
                "voltage_channels": {}, "voltage_spread_v": 0.0,
                "outlet_ma": {}, "current_available": False,
                "rssi_dbm": None, "raw": [],
                "note": "simulator - no voltage can be measured"}

    def _check_devid(self, devid):
        """Refuse a device id we cannot serve, instead of quietly ignoring it.

        Without this, asking the simulator for "NOPE" returns its state as if
        that device existed - exactly the kind of silent lie the multi-strip
        support must not introduce.
        """
        if devid and _norm_devid(devid) != "SIM":
            raise AdapterError(
                "no strip with device id %s is connected; the simulator answers "
                "as SIM only" % devid)

    def get_state(self, devid=None):
        self._check_devid(devid)
        with self._lock:
            st = self._load()
        # Same telemetry shape as the real adapter so every screen has the same
        # fields to read, but flagged so nothing can mistake it for hardware:
        # a simulator has no current, temperature or energy to report.
        empty = _telemetry_from(["0", "off"] + ["0"] * 10)
        empty["simulated"] = True
        empty["temp_c"] = None
        empty["power_w"] = 0.0
        empty["power_raw"] = 0
        empty["energy_kwh"] = 0.0
        empty["energy_raw"] = 0
        empty["state_code"] = "--"
        return [{"id": i, "on": st.get(i, False), **empty}
                for i in range(1, CHANNELS + 1)]

    def list_devices(self):
        return [{"devid": "SIM", "model": "MTTL-W01(simulator)", "connected": True,
                 "simulated": True, "remote": "in-process", "at": time.time()}]

    def current_device(self, devid=None):
        self._check_devid(devid)
        return "SIM"

    def set_switch(self, idx, on, devid=None):
        self._check_devid(devid)
        idx = int(idx)
        if not 1 <= idx <= CHANNELS:
            raise AdapterError("switch id must be 1..%d" % CHANNELS)
        with self._lock:
            st = self._load()
            st[idx] = bool(on)
            self._save(st)
        return {"id": idx, "on": bool(on), "device": "SIM"}

    def describe(self):
        return {"adapter": self.name, "simulated": True}

    def is_simulated(self):
        return True


class AutoAdapter(Adapter):
    """Keep the device listener open at all times and use the real strip as
    soon as one calls in; fall back to the simulator until then.

    This means a strip that has just been provisioned is picked up without
    restarting the service, while the UI, the app and Home Assistant stay
    usable in the meantime. Which one is live is reported, never faked.
    """

    name = "auto"

    def __init__(self, bind="0.0.0.0", device_port=DEVICE_PORT):
        self.mttl = MttlAdapter(bind=bind, port=int(device_port))
        self.sim = SimAdapter()

    @property
    def active(self):
        with self.mttl._lock:
            live = len(self.mttl._devices) > 0
        return self.mttl if live else self.sim

    def is_simulated(self):
        return self.active is self.sim

    def probe(self):
        p = self.mttl.probe()
        p["active"] = self.mttl.name if not self.is_simulated() else self.sim.name
        p["sim_fallback"] = self.is_simulated()
        return p

    def get_state(self, devid=None):
        a = self.active
        try:
            return a.get_state(devid)
        except AdapterError:
            # Fall back to the simulator only when no real strip exists at
            # all. A connected-but-unsettled strip must surface as an error
            # (so the controller reports unsettled), never as healthy sim data.
            with self.mttl._lock:
                live = len(self.mttl._devices) > 0
            if a is self.mttl and not devid and not live:
                return self.sim.get_state()
            raise

    def list_devices(self):
        """Real strips the listener has seen, plus the simulator as a fallback."""
        out = list(self.mttl.list_devices())
        if not any(d["connected"] for d in out):
            out.extend(self.sim.list_devices())
        return out

    def resolve(self, devid=None):
        with self.mttl._lock:
            live = len(self.mttl._devices) > 0
        if live:
            return self.mttl.resolve(devid)
        if devid:
            raise AdapterError(
                "no real MTTL strip is connected; cannot resolve %s" % devid)
        return None

    def current_device(self, devid=None):
        with self.mttl._lock:
            live = len(self.mttl._devices) > 0
        return self.mttl.current_device(devid) if live else self.sim.current_device(devid)

    def set_switch(self, idx, on, devid=None):
        if self.active is self.sim:
            raise AdapterError(
                "no real MTTL strip is connected; refusing to simulate a switch")
        return self.active.set_switch(idx, on, devid)

    def measure(self, devid=None, timeout=3.0):
        return self.active.measure(devid, timeout)

    def describe(self):
        return {"adapter": self.name, "active": self.active.name,
                "device": self.mttl.describe()}


def get_adapter(mode="auto", host=DEFAULT_HOST, port=DEFAULT_PORT,
                bind="0.0.0.0", device_port=DEVICE_PORT):
    """mode: 'auto' (default) real hardware as soon as a strip calls in, with
    the simulator as an explicit, clearly labelled fallback until then;
    'mttl' real hardware only, 'sim' simulator only, 'tcp' the old outbound
    guess (wrong way round for this device, kept only for reference).
    """
    if mode == "sim":
        return SimAdapter()
    if mode == "tcp":
        return TcpAdapter(host, port)
    if mode == "mttl":
        return MttlAdapter(bind=bind, port=int(device_port))
    return AutoAdapter(bind=bind, device_port=int(device_port))