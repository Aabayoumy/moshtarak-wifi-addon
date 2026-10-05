# Moshtarak WiFi — Home Assistant app (add-on)

Controls a **TONLY / LG-U+ MTTL-W01** four-socket Wi-Fi power strip from Home
Assistant.

The strip has no working local API and no vendor cloud that still works. It runs
a **callback protocol**: it connects *out* to whatever is listening on TCP 10086
and holds that socket open. Nothing can connect in to it. This add-on is that
listener.

## Contents

- `moshtarak_wifi/` — the app. Pure-standard-library Python, no pip install.
- `tests/` — the controller's assertion suites (169 assertions). Not shipped in
  the image; run them from a checkout.
- `tools/provision.py` — first-time strip provisioning. Deliberately **not** in
  the image: it must run on a machine joined to the strip's own setup Wi-Fi,
  which a container has no radio for. See [Provisioning](moshtarak_wifi/PROVISIONING.md).

## Install

1. **Settings → Apps → ⋮ (top right) → Repositories**
2. Add `https://github.com/Aabayoumy/moshtarak-wifi-addon`
3. **Settings → Apps → Install app → Moshtarak WiFi**, then Start
4. Then install the **`moshtarak_wifi`** integration from HACS. The add-on is
   the controller; the integration is what creates the entities in Home
   Assistant. Neither works alone.

## Options

| Option | Default | Meaning |
|---|---|---|
| `mode` | `auto` | `auto` = wait for a real strip, fall back to the simulator. `tcp` = real only. `sim` = simulator only. |
| `poll` | `5` | Seconds between state reads. |
| `protect` | *(empty)* | Firmware channels that may never be switched OFF. Legacy global list. |
| `protect_by_device` | *(empty)* | `DEV=ch,ch;DEV2=ch` — protection per strip. |
| `history_interval` | `20` | Seconds between history samples. |
| `history_keep_h` | `48` | Hours of history retained. |

### About `protect` vs `protect_by_device`

Keep **both** filled in if you use per-strip protection. When the per-strip map
is missing or unparseable the controller deliberately falls back to `protect`
rather than to "nothing protected" — so a typo there can never be the reason an
outlet holding a server becomes switchable. If you blank `protect`, you delete
that fail-safe.

## A warning you should read once

The strip's firmware numbers its channels differently from how the sockets are
physically arranged:

```
physical socket 1 -> firmware channel 2
physical socket 2 -> firmware channel 3
physical socket 3 -> firmware channel 4
physical socket 4 -> firmware channel 1
```

This is measured, not guessed. **Every user-facing surface in this project shows
physical socket numbers, and the firmware channel is internal detail.** It is
reported here and in `/api/config` so the mapping is visible, but nothing you
click should ever be driven by it. Getting this backwards switches the wrong
outlet — it caused the two most serious incidents in this project's history.

## Honest limits

Read these before you rely on a number:

- **Watts are not calibrated.** The vendor divides by 1000. A measured 60 W load
  read back as 16.75 W. Converted watts are always shown labelled unverified,
  with the raw integer beside them.
- **There is no current reading at all.** The strip exposes none, so watts can
  never be converted to amps. No current sensor is created, deliberately.
- **Volts are real.** ~206–215 V, read from an active query, drifting with mains
  load. Reported as a median across the four channels along with the spread.
- **Two status fields are not interpreted.** Fields 3 and 4 of the status block
  read `on` on empty sockets. They are not overload and not overheat, and this
  project does not pretend otherwise.
- **Right after a restart, state is not meaningful.** For ~10 s after the
  controller starts, `/api/state` reports all four sockets off with raw 0,
  including one that is very much powered. The relays do **not** open. The
  integration waits for `settled` before showing anything.

## Licence and provenance

The controller in `rootfs/` is Karim Elrashedy's work, used with permission, and
carries decisions recorded in the project history. The MTTL-W01 protocol was
recovered by decompiling the vendor's own Android app. See
[NOTICE.md](NOTICE.md) before redistributing.