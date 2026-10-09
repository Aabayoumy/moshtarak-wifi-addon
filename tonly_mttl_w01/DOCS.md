# MTTL-W01 WiFi — app configuration

Full option reference. For protocol background and honest limits, read the
[repository README](../README.md).

## Options

### `mode`

| Value | Behaviour |
|---|---|
| `auto` | Use a real strip if one connects; otherwise answer from the built-in simulator. **Default.** |
| `mttl` | Real hardware only. With no strip connected, state reads fail honestly instead of returning simulator data. |
| `tcp` | Legacy outbound guess (wrong way round for this device). Do not use. |
| `sim` | Simulator only. Useful for developing automations with no hardware. |

> **What `auto` really does with no hardware.** The simulator is always
> available, and it answers as device `SIM`. So in `auto` mode with no strip
> plugged in, `/api/state` reports four healthy sockets, `settled: true` and
> `reachable: true`. It is not lying — it is answering — but a client that
> trusts those flags alone would show four working switches that control nothing.
> The Home Assistant integration therefore checks for a **real** (non-simulated)
> strip explicitly, and will not present simulator output as your hardware. If
> you would rather the controller refuse outright, use `mttl`.

### `poll`

Seconds between state reads, default `2`. The controller caches for this long,
so several clients polling do not each cause a fresh read of the strip. Relay
state echoes back within 1–2 s, but **the physical relay can take up to 20 s to
close**, so do not use a switch reading as a guarantee that power has arrived.

### `order`

Commissioned unit D8AA59D270AA is identity `[1, 2, 3, 4]` (blink-verified 2026-10-09).
An earlier rotated reading `[2, 3, 4, 1]` was channel/socket confusion and is retired.
If your strip measures differently, set it via `POST /api/config` and record it in
the integration guard file `tonly_mttl_w01_socket_order.json`.

### `protect` / `protect_by_device`

Firmware channels that may never be driven OFF, whatever asks — the app, Home
Assistant, the web UI, an automation, a timer, a stray `curl`. A protected outlet
still reports its state; it just refuses to be turned off. Power can always be
restored.

`protect_by_device` takes `DEV=ch,ch;DEV2=ch` and locks per strip:

```
D8AA59D270AA=2  (identity order: socket 2 -> channel 2; server lives on socket 2)
```

A strip with no entry is deliberately unprotected — that is what "leave the
spare unlocked" means, and it is the entire reason the per-strip form exists.

**Fill in `protect` as well.** Resolution order is chosen so every uncertain path
lands on *protected*:

1. per-strip map present and readable → that strip's own list;
2. map absent or unreadable → the legacy `protect` list;
3. no strip named → the union of all locks.

Blanking `protect` means a typo in `protect_by_device` falls back to an **empty**
list — the one direction that must never happen. The controller logs one line
per boot, e.g. `protection: D8AA59D270AA locks socket [2]`. If you instead see
`!! PROTECTION FOR <id> MATCHES NO KNOWN STRIP`, those sockets are **not** locked.

### `history_interval` / `history_keep_h`

Sampling interval in seconds (default `20`) and retention in hours (default
`48`). Stored in `history.db` under `/config/tonly-mttl-w01/`, which is mapped so
it is included in Supervisor backups.

## State files

| Path | Contents |
|---|---|
| `/config/tonly-mttl-w01/config.json` | Runtime settings last saved through the API. **Beats the environment** — that is what the app last set. |
| `/config/tonly-mttl-w01/timers.json` | Timers. They run here, not in a phone app, so they fire when the phone is away. |
| `/config/tonly-mttl-w01/history.db` | SQLite history. |

## Networks

| Port | Reachable from | Why |
|---|---|---|
| `10086/tcp` | LAN | The strip dials out to this and holds it open. |
| `8099` | Add-on network only | The HTTP API. **No authentication** — anything that can reach it can drive the strip. Home Assistant reaches it here; do not publish it. |

## Checking it is alive

```sh
ha apps info tonly_mttl_w01      # state, ports, options
ha apps logs tonly_mttl_w01      # boot lines, protection, requests
```

From inside the add-on, `curl -s http://localhost:8099/api/health`. It answers
200 with zero strips connected, which is why it is safe as a watchdog target.