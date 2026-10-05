# Changelog

## 1.0.0

- Initial release.
- Packages Karim Elrashedy's Moshtarak-Wifi controller, **unmodified**, as a Home
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