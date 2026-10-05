#!/usr/bin/env python3
"""
Fake MTTL-W01 strip, used to prove the controller adapter and the provisioner
speak the real protocol before touching hardware.

Modes:
  --setup-port   act as the device in its setup access point (TCP 30300)
  --dial PORT    reboot onto the Wi-Fi and dial the controller on PORT,
                 send the hello, answer getinfo, accept onoff commands
"""
import argparse
import socket
import socketserver
import sys
import threading
import time

HELLO = "up:bootinfo:lgutap;2CFDB3355BA3;2cfdb3355ba3;0.1.54-1.0.66;connect"


def hello(devid="2CFDB3355BA3", model="lgutap", fw="0.1.54-1.0.66"):
    """The real device echoes its id twice, differing only in case."""
    return "up:bootinfo:%s;%s;%s;%s;connect" % (model, devid, devid.lower(), fw)


def status(sw, power=None, temp=25):
    """Build a getinfo status line.

    12 ';' fields per outlet. Fields 3 and 4 read 'on' on the real unit whatever
    is plugged in, so they are faked the same way rather than as meaningful
    flags - the adapter deliberately does not interpret them.

    Power follows the relay, as on real hardware: an open relay reports zero
    current. Without that the fixture could make a strip look like it is drawing
    power through a socket it has switched off, and a test would happily assert
    the impossible.
    """
    power = power or [0, 0, 0, 0]
    blocks = []
    for i in range(1, 5):
        st = "on" if sw[i - 1] else "off"
        raw = power[i - 1] if st == "on" else 0
        blocks.append("%d:%d;%s;3;on;on;%d;%08X;00000000;00000000;off;00;%d"
                      % (i, i, st, raw, 1234567 * i, temp))
    return "up:getinfo:" + ":".join(blocks)


class SetupHandler(socketserver.BaseRequestHandler):
    def handle(self):
        fh = self.request.makefile("rwb")
        print("[setup] strip: client connected from %s"
              % (self.client_address,), flush=True)
        for raw in fh:
            line = raw.decode("utf-8", "replace").strip()
            print("[setup] strip: << %r" % line, flush=True)
            if line.startswith("up:connect:"):
                ssid, _, pw = line[len("up:connect:"):].partition(":")
                print("[setup] strip: will join ssid=%r password=%r"
                      % (ssid, pw), flush=True)
            fh.write(b"OK\r\n")
            fh.flush()
            if line == "up:reboot:0":
                break
        fh.close()
        self.request.close()


class SetupServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def run_dial(port, host="127.0.0.1", devid="2CFDB3355BA3", fw="0.1.54-1.0.66",
             pattern="0000", power=None, temp=25):
    sw = [c == "1" for c in (pattern + "0000")[:4]]
    power = power or [0, 0, 0, 0]
    while True:
        try:
            s = socket.create_connection((host, port), timeout=5)
        except OSError as e:
            print("[strip] dial %s:%d failed: %s" % (host, port, e), flush=True)
            time.sleep(2)
            continue
        print("[strip] %s dialled controller at %s:%d" % (devid, host, port), flush=True)
        fh = s.makefile("rwb")
        try:
            # The real device holds this link open indefinitely; no read timeout.
            s.settimeout(None)
            fh.write((hello(devid, fw=fw) + "\r\n").encode())
            fh.flush()
            fh.write((status(sw, power, temp) + "\r\n").encode())
            fh.flush()
            for raw in fh:
                line = raw.decode("utf-8", "replace").strip()
                print("[strip] %s << %r" % (devid, line), flush=True)
                if line.startswith("up:onoff:"):
                    parts = line.split(":")
                    ch, st = parts[-2], parts[-1]
                    sw[int(ch) - 1] = (st == "on")
                    fh.write(("up:event:onoff:%s:%s\r\n" % (ch, st)).encode())
                    fh.flush()
                elif line == "up:getinfo:all":
                    fh.write((status(sw, power, temp) + "\r\n").encode())
                    fh.flush()
        except OSError as e:
            print("[strip] link ended: %s" % e, flush=True)
        finally:
            try:
                fh.close()
                s.close()
            except OSError:
                pass
        print("[strip] redialling", flush=True)
        time.sleep(2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup-port", type=int, default=0)
    ap.add_argument("--dial", type=int, default=0)
    ap.add_argument("--host", default="127.0.0.1")
    # Everything below exists so more than one strip can be faked at once, which
    # is the only way to prove the controller keeps their states apart.
    ap.add_argument("--devid", default="2CFDB3355BA3",
                    help="device id this strip announces (12 hex chars)")
    ap.add_argument("--fw", default="0.1.54-1.0.66")
    ap.add_argument("--pattern", default="0000",
                    help="initial relay states per firmware channel, e.g. 1010")
    ap.add_argument("--power", default="",
                    help="initial raw power per channel, comma separated, e.g. 100,0,14000,0")
    ap.add_argument("--temp", type=int, default=25)
    a = ap.parse_args()
    power = [int(x) for x in a.power.split(",")] if a.power else None
    if a.setup_port:
        srv = SetupServer(("0.0.0.0", a.setup_port), SetupHandler)
        print("[setup] strip listening on 0.0.0.0:%d" % a.setup_port, flush=True)
        if a.dial:
            threading.Thread(
                target=run_dial,
                args=(a.dial, a.host),
                kwargs=dict(devid=a.devid, fw=a.fw, pattern=a.pattern,
                            power=power, temp=a.temp),
                daemon=True).start()
        srv.serve_forever()
    elif a.dial:
        run_dial(a.dial, a.host, devid=a.devid, fw=a.fw, pattern=a.pattern,
                 power=power, temp=a.temp)
    else:
        ap.error("need --setup-port or --dial")


if __name__ == "__main__":
    main()