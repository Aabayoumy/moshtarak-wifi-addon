# NOTICE — read before redistributing

## The controller is someone else's work

`tonly_mttl_w01/rootfs/server.py` and `tonly_mttl_w01/rootfs/adapters.py` are
**Karim Elrashedy's** work on the Moshtarak-Wifi project, provided to this
repository to package as a Home Assistant app. They are used here **unmodified**
— the add-on adds a wrapper, an options mapping and a container, nothing to the
protocol logic.

That is deliberate. The controller has a test suite of 169 assertions and a
documented history of two serious incidents that a rewrite would have
reintroduced. A cleaner-looking replacement for a tested system is a downgrade.

If you fork this, keep that attribution. If you publish it, Karim's permission
covers the packaging; it is worth telling him the add-on exists.

## The protocol was recovered from a decompiled vendor app

The TONLY / LG-U+ MTTL-W01 wire protocol — the `up:bootinfo` / `up:getinfo` /
`up:onoff` command set, the 12-field status block layout — was recovered by
decompiling the vendor's own Android application, not from any published
documentation. There is no vendor SDK for this device.

Publishing reverse-engineered protocol implementations of a commercial product is
a legally and ethically grey area that varies by jurisdiction. This repository
makes no claim about what you may do with it. **The people who own the hardware
and the people who maintain this software are the ones who should be comfortable
with where it is hosted.** That is a judgement call about your own situation, not
something a licence file can settle for you.

Note that the Home Assistant **integration** in the companion repository
contains none of this: it is an HTTP client for the controller's REST API and
speaks no vendor protocol at all.

## Trademarks

TONLY, LG-U+ and MTTL-W01 are trademarks of their respective owners. This project
is not affiliated with, endorsed by, or supported by them. All product names are
used to identify the hardware this works with.