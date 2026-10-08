#!/usr/bin/env python3
"""
Moshtarak-Wifi - control service for the TONLY / MTTL-W01 multi-socket switch.

Exposes a small JSON HTTP API that both the Home Assistant integration and the
Android app use, hiding the device protocol behind adapters.get_adapter().

  GET  /api/health           service + adapter + device reachability
  GET  /api/state            current state of all four sockets
  POST /api/switch/<id>      {"on": true|false}          -> set one outlet
  POST /api/switches         {"1": true, "2": false}     -> set several
  GET  /api/config           current configuration, including socket order
  POST /api/config           {"names": [...], "order": [...]} -> persist labels
  GET  /api/timers           daily on/off schedule, run by this service
  POST /api/timers           add / update a timer
  POST /api/timers/delete    {"id": n}
  POST /api/timers/toggle    {"id": n, "enabled": bool}
  GET  /api/history?socket=&minutes=&points=   recorded power history
  GET  /api/diagnostics      controller health, devices, timers, history stats
  GET  /                     minimal web UI (works on any phone browser)

Standard library only, so there is nothing to install or keep updated.

SOCKET NUMBERING
----------------
The device firmware numbers its relays 1..4 in its own order, which is NOT the
order the sockets are physically arranged on the strip. On this unit the mapping
was measured and confirmed twice (an LED on the socket lit in step with the
firmware channel's current reading, and the server outlet stayed powered through
every test while the mapped channel was never driven):

    physical socket 1 -> firmware channel 2
    physical socket 2 -> firmware channel 3   <-- the server lives here
    physical socket 3 -> firmware channel 4
    physical socket 4 -> firmware channel 1

Everything user-facing (the API, the app, timers, history) uses PHYSICAL socket
numbers. The firmware channel is carried alongside as "channel" so nothing is
lost and a mismatch is visible rather than silent. CONFIG["order"] holds the
mapping and can be changed at runtime through POST /api/config.
"""
import json
import os
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adapters  # noqa: E402

SERVICE = "moshtarak-wifi"
STATE_DIR = os.environ.get("MOSHTARAK_WIFI_STATE", "/opt/moshtarak-wifi")
STARTED = time.time()


def env(name, default):
    """Read MOSHTARAK_WIFI_<name>, falling back to the legacy TONLY_<name>."""
    return os.environ.get("MOSHTARAK_WIFI_" + name,
                          os.environ.get("TONLY_" + name, default))


def _env_int_list(raw):
    out = []
    for x in (raw or "").replace(" ", "").split(","):
        if x.isdigit():
            out.append(int(x))
    return out


def _parse_protect_by_device(raw):
    """Parse "DEV=1,2;DEV2=3" into {"DEV": [1, 2], "DEV2": [3]}.

    Unparseable entries are skipped rather than guessed at. Skipping an entry
    means that device ends up with no protection of its own, which is why
    protected_channels_for() only trusts this map when it parsed to something
    non-empty.
    """
    out = {}
    for entry in (raw or "").split(";"):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        devid, _, chans = entry.partition("=")
        devid = devid.strip().upper()
        if not devid:
            continue
        vals = set()
        for c in chans.split(","):
            c = c.strip()
            if c.isdigit() and 1 <= int(c) <= adapters.CHANNELS:
                vals.add(int(c))
        if vals:
            out[devid] = sorted(vals)
    return out


def _default_order():
    # Measured on this strip: physical socket N is firmware channel N+1, wrapping
    # at 4. Written out longhand rather than computed so the mapping is visible
    # to anyone reading this file.
    return [2, 3, 4, 1]


CONFIG = {
    "mode": env("MODE", "auto"),                       # auto | tcp | sim
    "host": env("HOST", adapters.DEFAULT_HOST),
    "port": int(env("PORT", adapters.DEFAULT_PORT)),
    "bind": env("BIND", "0.0.0.0"),
    "listen": int(env("LISTEN", 8099)),
    # The strip dials *us* on this port once it has been provisioned, so this
    # is a listener, not a destination.
    "device_port": int(env("DEVICE_PORT", adapters.DEVICE_PORT)),
    "poll": float(env("POLL", "5")),                   # seconds between state polls
    # Which strip a request without ?device= refers to. Empty means "the most
    # recently connected one", so adding a second strip needs no reconfiguration.
    "device": env("DEVICE", "").strip(),
    # Physical socket order -> firmware channel. See the module docstring.
    "order": _env_int_list(env("ORDER", "")) or _default_order(),
    "names": [s.strip() for s in env(
        "NAMES", "Socket 1,Socket 2,Socket 3,Socket 4").split(",")],
    # Outlets that must never be switched, whatever asks: the app, Home
    # Assistant, the web UI, an automation, a stray curl, a timer. A protected
    # outlet still reports its state, it just refuses to be driven OFF.
    # These are FIRMWARE CHANNELS, matching what the device itself uses.
    "protect": sorted(set(_env_int_list(env("PROTECT", "")))),
    # Protection PER STRIP. Without this, a channel number meant on every strip:
    # socket 2 protected the server on strip one AND locked an empty socket on
    # every other strip, with no way to express "protect the server only".
    #
    # Format: DEV=ch,ch;DEV2=ch   (uppercase devid, firmware channels)
    # e.g.   2CFDB3355BA3=3
    #
    # Fail-safe by design: when this map is absent or unreadable the code falls
    # back to the legacy global list above rather than to "nothing protected".
    # A typo here must never be the reason a server's outlet became switchable.
    "protect_by_device": _parse_protect_by_device(env("PROTECT_BY_DEVICE", "")),
    # Timers run here rather than in the app so they fire when the phone is
    # away, asleep or has no network to the controller.
    "history_interval": float(env("HISTORY_INTERVAL", "20")),   # seconds
    "history_keep_h": float(env("HISTORY_KEEP_H", "48")),       # hours retained
}

_adapter = None
_cache = {"switches": {}, "at": {}}
_cache_lock = threading.Lock()
_cfg_lock = threading.RLock()
_last_error = {"where": None, "message": None, "at": None}


def note_error(where, exc):
    _last_error["where"] = where
    _last_error["message"] = str(exc)
    _last_error["at"] = time.time()


def adapter():
    global _adapter
    if _adapter is None:
        _adapter = adapters.get_adapter(CONFIG["mode"], CONFIG["host"],
                                        CONFIG["port"], CONFIG["bind"],
                                        CONFIG["device_port"])
        print("[moshtarak-wifi] adapter = %s" % _adapter.name, flush=True)
    return _adapter


# ---------------------------------------------------------------- socket order

def socket_to_channel(socket):
    """Physical socket number (1..4) -> firmware channel number."""
    try:
        ch = CONFIG["order"][socket - 1]
    except IndexError:
        raise ValueError("socket must be 1..%d" % adapters.CHANNELS)
    if not 1 <= ch <= adapters.CHANNELS:
        raise ValueError("order entry %r is not a valid firmware channel" % ch)
    return int(ch)


def channel_to_socket(ch):
    """Firmware channel -> physical socket number."""
    try:
        return CONFIG["order"].index(int(ch)) + 1
    except ValueError:
        return None


def switch_name(socket):
    try:
        return CONFIG["names"][socket - 1]
    except IndexError:
        return "Socket %d" % socket


def protected_channels_for(devid):
    """Firmware channels that must never be switched OFF on THIS strip.

    Resolution order, chosen so that every uncertain path lands on the
    PROTECTED answer rather than the switchable one:

      * per-device map present -> that device's own list, and no other device's.
        A device absent from the map is deliberately unprotected.
      * per-device map absent or empty (the pre-multi-strip layout, and what a
        rollback to the old file looks like) -> the legacy global list, so the
        server's outlet stays locked exactly as it was.
      * no device known at all -> the union of everything configured. Refusing to
        switch something off is recoverable; switching it off is not.
    """
    by = CONFIG.get("protect_by_device") or {}
    key = str(devid or "").strip().upper()
    if by:
        if key:
            return list(by.get(key, []))
        merged = set()
        for vals in by.values():
            merged.update(vals)
        return sorted(merged)
    return list(CONFIG["protect"])


def is_protected_channel(ch, devid=None):
    return int(ch) in protected_channels_for(devid)


def list_devices():
    """Every strip the listener knows about, connected or not."""
    try:
        return adapter().list_devices()
    except Exception as exc:
        note_error("list_devices", exc)
        return []


def decorate_devices(devs):
    """Attach each strip's own locks, in PHYSICAL socket numbers.

    Shared by /api/devices and /api/state so the two can never disagree about
    what is locked. A client listing strips needs this without asking a second
    question per strip - and asking per strip is not free: a strip that is not
    connected right now answers "unreachable" rather than its lock list, so a
    picker would show a blank exactly when you most want to see what is locked.

    Sockets, never firmware channels: this is the number someone can match to
    the socket in front of them.
    """
    out = []
    for d in (devs or []):
        d = dict(d)
        devid = str(d.get("devid") or "").strip().upper()
        chans = protected_channels_for(devid)
        d["protected_channels"] = chans
        d["protected_sockets"] = [c for c in (channel_to_socket(x) for x in chans)
                                  if c is not None]
        out.append(d)
    return out


def selected_device(explicit=None):
    """Which strip a bare request means.

    Explicit ?device= wins, then MOSHTARAK_WIFI_DEVICE, then the most recently
    connected strip (which is what resolve() does). With several strips up, the
    answer is reported in /api/state as "ambiguous" so nothing pretends the
    default was a deliberate choice.
    """
    return explicit or CONFIG["device"] or None


def state_is_settled(device=None):
    """True once a named strip has actually reported its sockets.

    Straight after the controller restarts, the strip re-dials but has not yet
    sent a status block. In that window read_state() yields an empty list, and a
    caller that reads "empty" as "no sockets" would draw four sockets that are
    all off - including the server's, which is very much on. That is a lie told
    by omission, so it is reported as "not ready" instead.
    """
    dev = device or ""
    with _cache_lock:
        return bool(_cache["switches"].get(dev))


def read_state(force=False, device=None):
    """Cached read so HA polling and the UI do not hammer the device.

    The cache is keyed by device: two strips connected at once must not share
    one cached answer.
    """
    dev = device or ""
    with _cache_lock:
        entry = _cache["switches"].get(dev)
        fresh = (time.time() - _cache["at"].get(dev, 0)) < CONFIG["poll"]
        if fresh and not force and entry:
            return entry
    try:
        sw = adapter().get_state(device)
    except Exception as exc:
        with _cache_lock:
            entry = _cache["switches"].get(dev)
            if entry:
                return entry
        note_error("read_state", exc)
        raise adapters.AdapterError(str(exc))
    out = []
    for s in sw:
        s = dict(s)
        physical = channel_to_socket(s["id"])
        s["channel"] = s["id"]
        s["socket"] = physical if physical is not None else s["id"]
        s["name"] = switch_name(s["socket"])
        s["reachable"] = True
        # Tell every surface (app, HA, web UI) that this outlet is locked, so it
        # can grey the control out instead of offering a button that would cut
        # something important.
        s["protected"] = is_protected_channel(s["id"], dev)
        out.append(s)
    # Present the list in physical order, which is what the sockets look like.
    out.sort(key=lambda s: s["socket"])
    with _cache_lock:
        _cache["switches"][dev] = out
        _cache["at"][dev] = time.time()
    return out


# --------------------------------------------------------------------- timers

TIMERS_FILE = os.path.join(STATE_DIR, "timers.json")
HISTORY_DB = os.path.join(STATE_DIR, "history.db")
CONFIG_FILE = os.path.join(STATE_DIR, "config.json")

_timers = []
_timers_lock = threading.RLock()
_timers_dirty = False


def _atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _save_config():
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        _atomic_write(CONFIG_FILE, json.dumps({
            "order": CONFIG["order"],
            "names": CONFIG["names"],
            "protect": CONFIG["protect"],
            # Persisted so a device that re-joins with a different id can be
            # reviewed rather than silently losing its lock.
            "protect_by_device": CONFIG.get("protect_by_device") or {},
            "device": CONFIG["device"],
        }, indent=1))
    except Exception as exc:
        note_error("save_config", exc)


def load_config():
    """Runtime-saved config beats the environment: it is what the app last set."""
    try:
        with open(CONFIG_FILE) as fh:
            data = json.load(fh)
    except Exception:
        return
    with _cfg_lock:
        order = data.get("order")
        if isinstance(order, list) and len(order) == adapters.CHANNELS and \
                all(isinstance(x, int) and 1 <= x <= adapters.CHANNELS for x in order) \
                and sorted(order) == list(range(1, adapters.CHANNELS + 1)):
            CONFIG["order"] = order
        names = data.get("names")
        if isinstance(names, list) and names:
            CONFIG["names"] = [str(x) for x in names]
        protect = data.get("protect")
        if isinstance(protect, list):
            CONFIG["protect"] = sorted({int(x) for x in protect
                                       if str(x).lstrip("-").isdigit()})
        # The per-device map is restored only if it parsed to something real. An
        # empty or broken value leaves the legacy list in charge, which is the
        # protected answer.
        by = data.get("protect_by_device")
        if isinstance(by, dict):
            restored = {}
            for devid, chans in by.items():
                vals = sorted({int(x) for x in (chans or [])
                               if str(x).isdigit() and 1 <= int(x) <= adapters.CHANNELS})
                if str(devid or "").strip() and vals:
                    restored[str(devid).strip().upper()] = vals
            if restored:
                CONFIG["protect_by_device"] = restored
        if "device" in data:
            CONFIG["device"] = str(data.get("device") or "").strip()


def load_timers():
    global _timers
    try:
        with open(TIMERS_FILE) as fh:
            data = json.load(fh)
        if isinstance(data, list):
            _timers = [t for t in data if isinstance(t, dict) and "socket" in t]
    except Exception:
        _timers = []


def save_timers():
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        _atomic_write(TIMERS_FILE, json.dumps(_timers, indent=1))
    except Exception as exc:
        note_error("save_timers", exc)


def timers_public():
    """Timers with the firmware channel resolved, plus what happens next."""
    out = []
    now = datetime_now()
    with _timers_lock:
        for t in sorted(_timers, key=lambda x: str(x.get("at"))):
            hhmm = str(t.get("at"))
            out.append({
                "id": t.get("id"),
                "socket": t.get("socket"),
                "channel": socket_to_channel_safe(t.get("socket")),
                "name": switch_name_safe(t.get("socket")),
                "device": t.get("device") or "",
                "at": hhmm,
                "on": bool(t.get("on")),
                "enabled": bool(t.get("enabled", True)),
                "label": t.get("label") or "",
                "protected": is_protected_channel_safe(t.get("socket"),
                                                       t.get("device")),
                # True when this timer would try to switch the server off, which
                # the service will refuse. Surfaced so the UI can say so up front.
                "will_be_refused": bool(t.get("on") is False and
                                        is_protected_channel_safe(t.get("socket"),
                                                                  t.get("device"))),
                "last_run": t.get("last_run"),
                "in_minutes": _next_occurrence_minutes(hhmm, now),
            })
    return out


def socket_to_channel_safe(socket):
    try:
        return socket_to_channel(int(socket))
    except Exception:
        return None


def is_protected_channel_safe(socket, devid=None):
    ch = socket_to_channel_safe(socket)
    return bool(ch is not None and is_protected_channel(ch, devid))


def switch_name_safe(socket):
    try:
        return switch_name(int(socket))
    except Exception:
        return "Socket %s" % socket


def _hhmm(s):
    try:
        hh, mm = str(s).split(":")[:2]
        h, m = int(hh), int(mm)
        if 0 <= h < 24 and 0 <= m < 60:
            return "%02d:%02d" % (h, m)
    except Exception:
        pass
    raise ValueError("time must be HH:MM")


def datetime_now():
    return time.localtime()


def _next_occurrence_minutes(hhmm, now=None):
    try:
        now = now or datetime_now()
        hh, mm = (int(x) for x in _hhmm(hhmm).split(":"))
        secs_now = now.tm_hour * 3600 + now.tm_min * 60 + now.tm_sec
        secs_at = hh * 3600 + mm * 60
        delta = secs_at - secs_now
        if delta < 0:
            delta += 86400
        return delta // 60
    except Exception:
        return None


def apply_timer(t):
    """Carry out one timer. Returns (ok, message)."""
    try:
        socket = int(t["socket"])
    except Exception:
        return False, "bad socket"
    ch = socket_to_channel(socket)
    on = bool(t.get("on"))
    device = t.get("device") or ""
    if is_protected_channel(ch, device) and not on:
        # Same rule as the HTTP API: a protected outlet can be brought UP but
        # never taken down. A timer must not be a back door around that.
        return False, ("physical socket %d (firmware channel %d) is protected - "
                       "will not be switched off" % (socket, ch))
    try:
        adapter().set_switch(ch, on, device or None)
        read_state(force=True, device=device or None)
        record_history(device or None)
        return True, "socket %d -> %s" % (socket, "on" if on else "off")
    except Exception as exc:
        note_error("timer", exc)
        return False, str(exc)


def timer_thread():
    """Fires daily timers. Survives restarts because the last run is on disk."""
    last_minute = None
    while True:
        try:
            now = datetime_now()
            minute_key = time.strftime("%Y-%m-%dT%H:%M", now)
            if last_minute != minute_key:
                last_minute = minute_key
                with _timers_lock:
                    due = [t for t in _timers
                           if t.get("enabled", True) and
                           _hhmm_safe(t.get("at")) == "%02d:%02d" % (now.tm_hour, now.tm_min)]
                for t in due:
                    ok, msg = apply_timer(t)
                    with _timers_lock:
                        t["last_run"] = int(time.time())
                        t["last_result"] = msg if ok else "refused: " + msg
                    save_timers()
                    print("[moshtarak-wifi] timer %s -> %s" % (t.get("label") or t.get("id"), msg),
                          flush=True)
        except Exception as exc:
            note_error("timer_thread", exc)
        time.sleep(5)


def _hhmm_safe(v):
    try:
        return _hhmm(v)
    except Exception:
        return None


# --------------------------------------------------------------------- history

def _db():
    os.makedirs(STATE_DIR, exist_ok=True)
    db = sqlite3.connect(HISTORY_DB, timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    # The relay column is deliberately NOT called "on": that is a reserved word
    # in SQLite and the CREATE TABLE fails with a bare "near on: syntax error"
    # that only shows up later, when history queries silently return nothing.
    db.execute("CREATE TABLE IF NOT EXISTS samples ("
               "ts INTEGER NOT NULL, socket INTEGER NOT NULL, ch INTEGER NOT NULL, "
               "relay INTEGER NOT NULL, power_raw INTEGER NOT NULL, temp_c INTEGER, "
               "dev TEXT NOT NULL DEFAULT '')")
    # An older database will not have the dev column; add it in place so history
    # recorded before a second strip existed is kept, not discarded.
    cols = {r[1] for r in db.execute("PRAGMA table_info(samples)").fetchall()}
    if "dev" not in cols:
        db.execute("ALTER TABLE samples ADD COLUMN dev TEXT NOT NULL DEFAULT ''")
    db.execute("CREATE INDEX IF NOT EXISTS ix ON samples(socket, ts)")
    db.commit()
    return db


def record_history(device=None):
    """Append one row per socket. Never raises into the caller."""
    try:
        sw = read_state(device=device)
    except Exception:
        return
    now = int(time.time())
    # Which strip these readings came from, so two strips do not silently merge
    # into one history.
    label = (device or "").upper()
    if not label:
        try:
            label = str(adapter().resolve(None).devid or "").upper()
        except Exception:
            label = ""
    rows = [(now, s["socket"], s.get("channel", s["id"]), 1 if s["on"] else 0,
             int(s.get("power_raw") or 0), s.get("temp_c"), label)
            for s in sw]
    try:
        db = _db()
        db.executemany("INSERT INTO samples VALUES (?,?,?,?,?,?,?)", rows)
        db.commit()
        db.close()
    except Exception as exc:
        note_error("record_history", exc)


def history_thread():
    keep = max(1.0, CONFIG["history_keep_h"]) * 3600
    while True:
        time.sleep(max(5.0, CONFIG["history_interval"]))
        record_history()
        try:
            db = _db()
            db.execute("DELETE FROM samples WHERE ts < ?", (int(time.time() - keep),))
            db.commit()
            db.close()
        except Exception as exc:
            note_error("prune_history", exc)


def history_query(socket=None, minutes=180, points=120, device=None):
    try:
        minutes = max(1, min(int(minutes), 60 * 24 * 14))
    except Exception:
        minutes = 180
    try:
        points = max(2, min(int(points), 2000))
    except Exception:
        points = 120
    since = int(time.time() - minutes * 60)
    dev = (device or "").upper()
    try:
        db = _db()
        if socket:
            if dev:
                rows = db.execute(
                    "SELECT ts, relay, power_raw, temp_c FROM samples "
                    "WHERE socket=? AND dev=? AND ts>=? ORDER BY ts",
                    (int(socket), dev, since)).fetchall()
            else:
                rows = db.execute(
                    "SELECT ts, relay, power_raw, temp_c FROM samples "
                    "WHERE socket=? AND ts>=? ORDER BY ts", (int(socket), since)).fetchall()
        else:
            rows = []
            for s in (1, 2, 3, 4):
                if dev:
                    rows += [(s,) + r for r in db.execute(
                        "SELECT ts, relay, power_raw, temp_c FROM samples "
                        "WHERE socket=? AND dev=? AND ts>=? ORDER BY ts",
                        (s, dev, since)).fetchall()]
                else:
                    rows += [(s,) + r for r in db.execute(
                        "SELECT ts, relay, power_raw, temp_c FROM samples "
                        "WHERE socket=? AND ts>=? ORDER BY ts", (s, since)).fetchall()]
        db.close()
    except Exception as exc:
        note_error("history_query", exc)
        return []
    if not rows:
        return []
    if socket:
        # Downsample to the requested number of points by bucketing on time.
        first, last = rows[0][0], rows[-1][0]
        span = max(1, last - first)
        buckets = {}
        for r in rows:
            b = min(points - 1, int((r[0] - first) * points / span))
            buckets.setdefault(b, []).append(r)
        out = []
        for b in sorted(buckets):
            grp = buckets[b]
            power = [g[2] for g in grp if g[1]]
            out.append({
                "ts": grp[-1][0],
                "on": bool(grp[-1][1]),
                "power_raw": max(power) if power else 0,
                "temp_c": grp[-1][3],
            })
        return out
    return [{"socket": r[0], "ts": r[1], "on": bool(r[2]),
             "power_raw": r[3], "temp_c": r[4]} for r in rows]


def history_stats():
    try:
        db = _db()
        total = db.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        newest = db.execute("SELECT MAX(ts) FROM samples").fetchone()[0]
        oldest = db.execute("SELECT MIN(ts) FROM samples").fetchone()[0]
        per = db.execute("SELECT socket, COUNT(*) FROM samples GROUP BY socket").fetchall()
        db.close()
        return {"rows": total,
                "oldest": oldest,
                "newest": newest,
                "fresh_seconds": int(time.time() - newest) if newest else None,
                "per_socket": {str(a): b for a, b in per}}
    except Exception as exc:
        note_error("history_stats", exc)
        return {"rows": 0, "oldest": None, "newest": None,
                "fresh_seconds": None, "per_socket": {}}


# ----------------------------------------------------------------- diagnostics

def diagnostics():
    a = adapter()
    try:
        probe = a.probe()
    except Exception as exc:
        probe = {"error": str(exc)}
    devs = list_devices()
    connected = [d for d in devs if d.get("connected")]
    # The strip this report is about. Named once and reused, because the socket
    # list and the protection list must not describe different strips.
    dev = CONFIG["device"] or ""
    try:
        sw = read_state(device=dev or None)
        state_err = None
    except Exception as exc:
        sw = []
        state_err = str(exc)
    return {
        "service": SERVICE,
        "adapter": a.name,
        "uptime_seconds": int(time.time() - STARTED),
        "started": int(STARTED),
        "simulated": a.is_simulated(),
        "device": probe,
        "device_port": CONFIG["device_port"],
        "state_error": state_err,
        "strips": devs,
        "strips_connected": len(connected),
        "selected": CONFIG["device"] or "",
        "sockets": [{
            "socket": s["socket"], "channel": s["channel"], "name": s["name"],
            "on": s["on"], "protected": s["protected"],
            "power_raw": s.get("power_raw"), "power_w": s.get("power_w"),
            "temp_c": s.get("temp_c"), "state_code": s.get("state_code"),
        } for s in sw],
        "protection": {
            # Firmware channels, and the physical socket each one is, so there
            # is never any doubt which real-world socket is locked.
            #
            # Scoped to the strip being asked about. Reporting the global list
            # here is what made a second strip look like it had the server's
            # lock on it.
            "device": dev,
            "channels": protected_channels_for(dev),
            "sockets": [c for c in (channel_to_socket(x)
                                    for x in protected_channels_for(dev))
                        if c is not None],
        },
        # The whole map, so a client can show every strip's locks at once.
        "protection_by_device": CONFIG.get("protect_by_device") or {},
        "order": CONFIG["order"],
        "names": CONFIG["names"],
        "timers": timers_public(),
        "history": history_stats(),
        # Sampling period, so a client can explain an empty window accurately
        # instead of guessing how often a sample should have appeared.
        "history_interval_s": int(CONFIG["history_interval"]),
        "history_keep_h": CONFIG["history_keep_h"],
        # None means "nothing has gone wrong since the service started", which
        # is a different statement from "something went wrong and we have no
        # message for it". A dict full of nulls reads as the second and would
        # make every client show an error that never happened.
        "last_error": dict(_last_error) if _last_error["at"] else None,
        "time": int(time.time()),
    }


PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Moshtarak-Wifi</title>
<style>
 body{font-family:system-ui,sans-serif;background:#12151a;color:#e8ecf1;margin:0;
      padding:20px;max-width:560px;margin-inline:auto}
 h1{font-size:20px;margin:0 0 4px}
 h2{font-size:14px;color:#8b97a8;margin:22px 0 8px;text-transform:uppercase;
    letter-spacing:.06em}
 .sub{color:#8b97a8;font-size:13px;margin-bottom:18px}
 .row{display:flex;align-items:center;justify-content:space-between;background:#1b2029;
      border-radius:12px;padding:14px 16px;margin-bottom:10px}
 .n{font-weight:600}
 .s{font-size:12px;color:#8b97a8}
 button{min-width:88px;padding:10px 14px;border-radius:9px;border:0;font-size:15px;
        font-weight:600;cursor:pointer;background:#2a3140;color:#cfd8e3}
 button.on{background:#1f9d55;color:#fff}
 .err{background:#5a2020;padding:10px;border-radius:9px;margin-bottom:12px;font-size:13px}
 .warn{color:#e0a355}
</style></head><body>
<h1>Moshtarak-Wifi multi-socket strip</h1>
<div class=sub id=sub>...</div>
<div id=err></div>
<h2>Sockets</h2>
<div id=list></div>
<h2>USB section</h2>
<div class=note>The USB ports sit on their own charger board with no verified
independent relay. There is no command to switch them, so there is no button
here pretending otherwise. Power runs whenever the strip itself has mains.</div>
<h2>Notes</h2>
<div class=note>Power figures are the device's own reading on the vendor scale
(raw / 1000). That scale is <b>unverified</b>: a load labelled 60 W read 16.75
on it. Watts are shown because the number is real, not because the scale is
confirmed.</div>
<script>
async function refresh(){
  try{
    const r=await fetch('/api/state',{cache:'no-store'});
    const j=await r.json();
    document.getElementById('err').innerHTML='';
    document.getElementById('sub').textContent=
      (j.adapter||'')+' - '+(j.reachable?'device reachable':'device NOT reachable');
    document.getElementById('list').innerHTML=(j.switches||[]).map(s=>
      `<div class=row><div><div class=n>Socket ${s.socket} &middot; ${s.name}</div>`+
      `<div class=s>firmware channel ${s.channel} - ${s.on?'ON':'OFF'}`+
      ` - ${s.power_w.toFixed(2)} W (raw ${s.power_raw})`+
      ` - ${s.temp_c}&deg;C${s.protected?' - PROTECTED':''}</div></div>`+
      (s.protected
        ? `<button class=on disabled style="opacity:.55">${s.on?'ON':'OFF'}</button>`
        : `<button class="${s.on?'on':''}" onclick="tog(${s.socket},${!s.on})">`+
          `${s.on?'ON':'OFF'}</button>`)+`</div>`).join('');
  }catch(e){document.getElementById('err').innerHTML=
    '<div class=err>'+e+'</div>';}
}
async function tog(socket,on){
  const r=await fetch('/api/switch/socket/'+socket,{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({on:on})});
  if(r.status===409){alert('This socket is protected and will not be switched off.');}
  refresh();
}
refresh(); setInterval(refresh,4000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "moshtarak-wifi/2.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("[moshtarak-wifi] %s - %s" % (self.address_string(), fmt % args), flush=True)

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, code, text):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except Exception:
            return {}

    def _qs(self):
        out = {}
        for pair in self.path.split("?", 1)[1].split("&") if "?" in self.path else []:
            if "=" in pair:
                k, v = pair.split("=", 1)
                out[k] = v
        return out

    # -- read -------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/":
            return self._html(200, PAGE)
        if path == "/api/health":
            a = adapter()
            probe = a.probe()
            return self._json(200, {"ok": True, "service": SERVICE,
                                    "config": {k: CONFIG[k] for k in
                                               ("mode", "host", "port", "listen",
                                                "device_port", "poll", "protect",
                                                "order", "device")},
                                    "adapter": a.name, "device": probe,
                                    "strips_connected": len(
                                        [d for d in list_devices() if d.get("connected")]),
                                    "time": int(time.time())})
        if path == "/api/state":
            a = adapter()
            devs = list_devices()
            want = selected_device(self._qs().get("device"))
            connected = [d for d in devs if d.get("connected")]
            # Which strip this answer is about. Resolved BEFORE the read and
            # defaulted to what was asked for, so the success path and the
            # failure path below can both name it. Deriving it inside the try
            # meant the error path could reach a name that had never been
            # assigned, and the request died with an UnboundLocalError instead
            # of the honest "unreachable" answer.
            try:
                which = str(a.current_device(want) or "") or str(want or "")
            except Exception:
                which = str(want or "")
            # Locks for the strip actually being answered. `which`, not `want`:
            # `want` is empty whenever the controller picked the strip itself,
            # which is the normal single-strip case.
            locks = protected_channels_for(which)
            # False only in the short window after a controller restart, when the
            # strip has re-dialled but has not yet reported. A client should show
            # "connecting" rather than read an empty socket list as four outlets
            # that are all off.
            body = {
                "ok": True,
                "adapter": a.name,
                "simulated": a.is_simulated(),
                "order": CONFIG["order"],
                "device": which,
                "devices_connected": len(connected),
                # True when several strips are up and the caller did not name one.
                # Not an error, but the app says so rather than pretending the
                # choice was made.
                "ambiguous": len(connected) > 1 and not want,
                "devices": decorate_devices(devs),
                "settled": state_is_settled(want),
                "protection": {
                    "device": which,
                    "channels": locks,
                    "sockets": [c for c in (channel_to_socket(x) for x in locks)
                                if c is not None],
                },
                # The whole map, so a client can show every strip's locks at once.
                "protection_by_device": CONFIG.get("protect_by_device") or {},
            }
            try:
                sw = read_state(device=want)
                body["reachable"] = True
                body["sockets"] = body["switches"] = sw
                return self._json(200, body)
            except Exception as exc:
                # Unreachable is a normal answer, not a crash: the strip may be
                # rebooting, or the caller named one that is not connected.
                body["ok"] = False
                body["reachable"] = False
                body["simulated"] = False
                body["error"] = str(exc)
                body["settled"] = False
                body["sockets"] = body["switches"] = []
                return self._json(200, body)

        if path == "/api/devices":
            devs = list_devices()
            return self._json(200, {
                "ok": True,
                "devices": decorate_devices(devs),
                "protection_by_device": CONFIG.get("protect_by_device") or {},
                "connected": len([d for d in devs if d.get("connected")]),
                "selected": selected_device(self._qs().get("device")) or "",
                "default": CONFIG["device"],
                "listener": "%s:%d" % (CONFIG["bind"], CONFIG["device_port"]),
            })
        if path == "/api/config":
            with _cfg_lock:
                return self._json(200, dict(CONFIG, ok=True))
        if path == "/api/timers":
            return self._json(200, {"ok": True, "timers": timers_public(),
                                    "protection": CONFIG["protect"]})
        if path == "/api/history":
            q = self._qs()
            return self._json(200, {
                "ok": True,
                "socket": int(q["socket"]) if "socket" in q else None,
                "device": q.get("device") or "",
                "minutes": int(q.get("minutes", 180)),
                "samples": history_query(q.get("socket"), q.get("minutes", 180),
                                         q.get("points", 120), q.get("device")),
            })
        if path == "/api/diagnostics":
            return self._json(200, dict(diagnostics(), ok=True))
        if path == "/api/probe":
            # READ-ONLY. Sends only the strip's measurement queries and returns
            # what it says. No onoff is reachable from this route, so it cannot
            # change an outlet - including a protected one.
            dev = self._qs().get("device") or None
            try:
                return self._json(200, dict(adapter().measure(devid=dev), ok=True))
            except adapters.AdapterError as exc:
                return self._json(409, {"ok": False, "error": str(exc)})
        return self._json(404, {"ok": False, "error": "not found"})

    # -- write ------------------------------------------------------
    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        body = self._body()
        try:
            return self._post(path, body)
        except ValueError as exc:
            return self._json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            note_error(path, exc)
            return self._json(500, {"ok": False, "error": str(exc)})

    def _post(self, path, body):
        device = selected_device(self._qs().get("device"))
        # --- single socket, by PHYSICAL socket number (what the app shows) ---
        if path.startswith("/api/switch/socket/"):
            try:
                idx = int(path.rsplit("/", 1)[1])
                channel = socket_to_channel(idx)
            except ValueError:
                return self._json(400, {"ok": False, "error": "bad socket number"})
            return self._drive(channel, body, idx, device)

        # --- single socket, by firmware channel (legacy, used by HA) ---
        if path.startswith("/api/switch/"):
            try:
                idx = int(path.rsplit("/", 1)[1])
            except ValueError:
                return self._json(400, {"ok": False, "error": "bad id"})
            return self._drive(idx, body, None, device)

        if path == "/api/switches":
            results = []
            for k, v in body.items():
                try:
                    ch = int(k)
                except (TypeError, ValueError):
                    results.append({"id": k, "error": "bad id"})
                    continue
                ok, msg = self._apply(ch, bool(v), device)
                results.append({"id": ch, "ok": ok, "error": msg})
            read_state(force=True, device=device)
            record_history(device)
            return self._json(200, {"ok": True, "results": results})

        if path == "/api/config":
            with _cfg_lock:
                changed = {}
                if "names" in body and isinstance(body["names"], list):
                    CONFIG["names"] = [str(x) for x in body["names"]][:adapters.CHANNELS]
                    changed["names"] = CONFIG["names"]
                if "order" in body and isinstance(body["order"], list):
                    order = [int(x) for x in body["order"]]
                    if len(order) != adapters.CHANNELS or \
                            sorted(order) != list(range(1, adapters.CHANNELS + 1)):
                        raise ValueError(
                            "order must be a permutation of 1..%d" % adapters.CHANNELS)
                    CONFIG["order"] = order
                    changed["order"] = order
                if "protect" in body and isinstance(body["protect"], list):
                    CONFIG["protect"] = sorted({int(x) for x in body["protect"]
                                               if str(x).lstrip("-").isdigit()})
                    changed["protect"] = CONFIG["protect"]
                if "protect_by_device" in body and isinstance(body["protect_by_device"], dict):
                    restored = {}
                    for devid, chans in body["protect_by_device"].items():
                        vals = sorted({int(x) for x in (chans or [])
                                       if str(x).isdigit()
                                       and 1 <= int(x) <= adapters.CHANNELS})
                        if str(devid or "").strip() and vals:
                            restored[str(devid).strip().upper()] = vals
                    # Replacing the map wholesale. An empty dict is honoured, so a
                    # client can deliberately clear every lock - but only the map,
                    # never the legacy list, which stays as the fallback.
                    CONFIG["protect_by_device"] = restored
                    changed["protect_by_device"] = restored
                if "device" in body:
                    CONFIG["device"] = str(body.get("device") or "").strip()
                    changed["device"] = CONFIG["device"]
                _save_config()
            # Order and protection change what /api/state means, so every cached
            # answer is now stale - not just the default device's.
            with _cache_lock:
                _cache["switches"].clear()
                _cache["at"].clear()
            return self._json(200, {"ok": True, "changed": changed,
                                    "config": CONFIG})

        if path == "/api/timers":
            socket = int(body["socket"])
            ch = socket_to_channel(socket)
            at = _hhmm(body.get("at"))
            on = bool(body.get("on", True))
            entry = {"socket": socket, "channel": ch, "at": at, "on": on,
                     "enabled": bool(body.get("enabled", True)),
                     "device": str(body.get("device") or ""),
                     "label": str(body.get("label") or "")[:60]}
            with _timers_lock:
                if body.get("id") is not None:
                    for i, t in enumerate(_timers):
                        if t.get("id") == body["id"]:
                            entry["id"] = body["id"]
                            entry["last_run"] = t.get("last_run")
                            _timers[i] = entry
                            break
                    else:
                        entry["id"] = int(time.time() * 1000) % 100000000
                        _timers.append(entry)
                else:
                    entry["id"] = int(time.time() * 1000) % 100000000
                    _timers.append(entry)
            save_timers()
            return self._json(200, {"ok": True, "timers": timers_public()})

        if path == "/api/timers/delete":
            tid = body.get("id")
            with _timers_lock:
                before = len(_timers)
                _timers[:] = [t for t in _timers if t.get("id") != tid]
                removed = before - len(_timers)
            save_timers()
            return self._json(200, {"ok": True, "removed": removed,
                                    "timers": timers_public()})

        if path == "/api/timers/toggle":
            tid = body.get("id")
            hit = None
            with _timers_lock:
                for t in _timers:
                    if t.get("id") == tid:
                        t["enabled"] = bool(body.get("enabled", not t.get("enabled", True)))
                        hit = dict(t)
                        break
            if hit is None:
                return self._json(404, {"ok": False, "error": "no such timer"})
            save_timers()
            return self._json(200, {"ok": True, "timers": timers_public()})

        return self._json(404, {"ok": False, "error": "not found"})

    def _apply(self, channel, on, device=None):
        """Shared switch path. Returns (ok, message)."""
        # Refuse to turn a protected outlet OFF. This is the only direction that
        # can cut something important: switching one ON only ever adds power, and
        # allowing ON means the outlet can still be restored after a power cut
        # without anyone having to edit this file.
        if is_protected_channel(channel, device) and not on:
            return False, ("firmware channel %d (physical socket %s) is protected "
                           "and will not be switched off"
                           % (channel, channel_to_socket(channel)))
        try:
            adapter().set_switch(channel, on, device)
            return True, "ok"
        except Exception as exc:
            note_error("set_switch", exc)
            return False, str(exc)

    def _drive(self, channel, body, physical, device=None):
        if "on" not in body:
            return self._json(400, {"ok": False, "error": "missing 'on'"})
        ok, msg = self._apply(channel, bool(body["on"]), device)
        if not ok:
            code = 409 if is_protected_channel(channel, device) else 502
            return self._json(code, {
                "ok": False, "error": msg,
                "switch": {"id": channel,
                           "socket": physical if physical is not None else channel_to_socket(channel),
                           "protected": is_protected_channel(channel, device)}})
        read_state(force=True, device=device)
        record_history(device)
        return self._json(200, {"ok": True, "switch": {"id": channel, "socket": physical,
                                                        "on": bool(body["on"])}})


def _known_real_devices():
    """Device ids of REAL strips the listener has seen.

    In auto mode the adapter lists the in-process simulator alongside any real
    strip, and the simulator answers as the id "SIM". Counting it here would make
    every configured hardware id look like a typo on every single restart, and a
    warning that always fires is a warning nobody reads.
    """
    out = set()
    for d in (list_devices() or []):
        if d.get("simulated"):
            continue
        devid = str(d.get("devid") or "").strip().upper()
        if devid:
            out.add(devid)
    return out


def _warn_about_protection_typos():
    """Say loudly if a protected device id is not one we have ever seen.

    The failure mode this guards is specific and nasty: protection is keyed by
    device id, so an id with a typo in it silently protects nothing, and the
    symptom is a server outlet that has quietly become switchable. A loud line
    in the journal turns that into something noticed at boot.
    """
    by = CONFIG.get("protect_by_device") or {}
    if not by:
        if CONFIG["protect"]:
            print("[moshtarak-wifi] protection: legacy global list %s applies to "
                  "EVERY strip - socket %s protected on all of them"
                  % (CONFIG["protect"],
                     [c for c in (channel_to_socket(x) for x in CONFIG["protect"])
                      if c is not None]), flush=True)
        return
    try:
        known = _known_real_devices()
    except Exception as exc:
        print("[moshtarak-wifi] protection self-check skipped: %s" % exc,
              flush=True)
        return
    if not known:
        # No real strip has said hello yet, so every configured id would look like
        # a typo. Saying so here is noise, and noise is how a real warning gets
        # ignored. The deferred re-check waits for a strip to actually appear.
        return
    for devid, chans in sorted(by.items()):
        socks = [c for c in (channel_to_socket(x) for x in chans) if c is not None]
        if devid not in known:
            print("[moshtarak-wifi] !! PROTECTION FOR %s MATCHES NO KNOWN STRIP - "
                  "those sockets are NOT locked. Known: %s"
                  % (devid, sorted(known) or "none yet"), flush=True)
        else:
            print("[moshtarak-wifi] protection: %s locks socket %s"
                  % (devid, socks), flush=True)


def _deferred_protection_check():
    """Re-run the protection check once a strip has actually registered.

    At boot the listener has no devices, so the only moment a mismatched device
    id becomes visible is after the first strip says hello. Checking only at
    start-up would either warn about everything or warn about nothing.
    """
    for _ in range(60):                      # ~5 minutes at 5s
        time.sleep(5)
        try:
            # Real strips only, and only ones actually connected: this is the
            # earliest moment a mismatched id can actually be spotted.
            if _known_real_devices():
                _warn_about_protection_typos()
                return
        except Exception:
            continue
    # Nothing real turned up in five minutes. Now the silence IS the finding:
    # a protected strip that never registers means its sockets are not locked,
    # so say so rather than leaving it to be discovered.
    print("[moshtarak-wifi] !! no real strip has registered after 5 minutes; "
          "every configured lock is UNCONFIRMED", flush=True)


def main():
    print("[moshtarak-wifi] starting on %s:%d mode=%s device=%s:%d"
          % (CONFIG["bind"], CONFIG["listen"], CONFIG["mode"],
             CONFIG["host"], CONFIG["port"]), flush=True)
    load_config()
    load_timers()
    _warn_about_protection_typos()
    threading.Thread(target=_deferred_protection_check, name="protectcheck",
                     daemon=True).start()
    # Bind the device listener at start-up rather than on the first API
    # request: a strip that has just rebooted onto the Wi-Fi dials us straight
    # away, so the socket must already be open before anything queries the API.
    adapter()
    # Timers and history are background work that must not be able to take the
    # API down with them.
    threading.Thread(target=timer_thread, name="timers", daemon=True).start()
    threading.Thread(target=history_thread, name="history", daemon=True).start()
    record_history()
    ThreadingHTTPServer((CONFIG["bind"], CONFIG["listen"]), Handler).serve_forever()


if __name__ == "__main__":
    main()