# Changelog

## 1.0.3

- **Renamed**: the app is now `tonly_mttl_w01` (TONLY MTTL-W01 WiFi). The old
  `moshtarak-wifi` name and repository are gone. The controller itself remains
  Karim Elrashedy's unmodified code; the vendored `MOSHTARAK_WIFI_*`
  environment names were renamed to `TONLY_MTTL_W01_*` (breaking: reconfigure
  or rely on the one-shot `/config` migration in run.sh).
- **Switch state now echoes in ~2s instead of ~10s.** The controller force-reads
  state immediately after a switch command, before the strip's echo lands, and
  serves that stale reading from a `poll`-second cache. The default `poll` is
  now 2s (was 5s), and the Home Assistant integration re-reads once ~2.5s after
  a command, so the UI reflects the strip's own answer a couple of seconds
  after a tap instead of on a later poll cycle.

## 1.0.0

- Initial release.
- Packages Karim Elrashedy's MTTL-W01 WiFi controller, **unmodified**, as a Home
  Assistant app. No protocol logic was changed.
- Options map onto the controller's existing environment variables: `mode`,
  `poll`, `protect`, `protect_by_device`, `history_interval`, `history_keep_h`.
- Publishes only TCP 10086 to the LAN, because that is the port the strip dials
  out to. The HTTP API stays on the add-on network because it has no
  authentication.
- Watchdog points at `/api/health`, which answers 200 with zero strips connected,
  so an install with no hardware yet cannot restart-loop.
- `provision.py` is shipped in `tools/` rather than in the image: it needs a
  machine that can join the strip's own setup Wi-Fi.
## 1.0.1

Fixes found by actually installing the app rather than reading the Dockerfile.

- **The image could not build.** The build-time smoke test used a multi-line
  `python3 -c "..."` inside a `RUN`. Docker continues a line only on an
  explicit backslash, so the second physical line was parsed as an instruction
  and the build failed with `dockerfile parse error ... unknown instruction:
  import`. Both checks are now single physical lines, and
  `tests/test_addon_contract.py` now rejects a Dockerfile whose logical lines
  do not each begin with a real instruction.

- **The app could not start.** `USER tonly` was set in the Dockerfile.
  s6-overlay v3 needs to begin as root so it can set up supervision and drop
  privileges itself; with `USER` set the app died on every start with
  `s6-overlay-suexec: fatal: can only run as pid 1`. There was a second reason
  it could not have worked: `TONLY_MTTL_W01_STATE` is `/config/tonly-mttl-w01`,
  and `/config` is a root-owned bind mount supplied by the Supervisor at run
  time, so an unprivileged process could not create it. The app now runs as
  root, as most Home Assistant apps do, and the Dockerfile records why.

- **Replaced the weak build checks with a real one.** The old checks confirmed a
  user existed and could bind a socket; both were true of an image that still
  could not start. `tonly_mttl_w01/build_verify.py` now launches the controller
  inside the build and requires a 200 from `/api/health`, checks that the mode
  reached it from the environment, and fails the build if the commissioned
  socket→channel order is not `[1, 2, 3, 4]` (identity, blink-verified 2026-10-09).

## 1.0.2

- **The app still would not start**, even at 1.0.1. This was the real cause, and
  it was not in the Dockerfile at all.

  The Home Assistant base image sets `ENTRYPOINT ["/init"]`, which is
  s6-overlay v3. s6-overlay only functions when it genuinely is PID 1: it
  installs signal handlers, reaps zombies, and hands the container `CMD` to
  `s6-overlay-suexec`, which refuses to run as anything else.

  The Supervisor passes an app's `init` key straight through to Docker's
  `--init` flag, and it **defaults to true**. With `--init`, Docker injects
  tini as PID 1, so `/init` runs as a child of tini and the app died on every
  start with `s6-overlay-suexec: fatal: can only run as pid 1`. Setting
  `init: false` stops Docker injecting anything, `/init` becomes PID 1, and the
  CMD runs under s6 supervision as intended. Core add-ons that ship a main
  daemon — mosquitto among them — set this too.

  Worth recording honestly: removing `USER` in 1.0.1 was a real bug (see below)
  but it was not the cause of the startup failure, and the app failed
  identically with it gone. Two plausible-sounding fixes in a row, and neither
  was the answer, is what sent me reading the base image's actual `Entrypoint`
  from the registry instead of reasoning about it.

- `tests/test_addon_contract.py` asserts `init: false`, that no `ENTRYPOINT` is
  overridden, and that an exec-form `CMD` exists — 3 more checks aimed at exactly
  this failure.
