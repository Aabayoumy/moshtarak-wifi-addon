# Provisioning a strip (one time per strip)

The add-on **cannot** do this. `tools/provision.py` has to run on a machine that
can join the strip's own setup Wi-Fi, and a container has no radio. That is a
real limitation, not an oversight.

## What provisioning does

A fresh strip boots into its own access point and answers TCP 30300. Three
plain-text lines move it onto your network and tell it where to call back:

```
up:ip:<the address the strip should dial>\r\n
up:connect:<ssid>:<password>\r\n
up:reboot:0\r\n
```

CRLF, ASCII, that order. The setup dialect is colon-separated, so **your SSID and
password must not contain a colon.** The script refuses rather than sending
something the strip will misparse.

After `up:reboot:0` the strip reboots onto your Wi-Fi and dials
`<the address you gave it>:10086`, where the add-on is already listening.

## Steps (automatic)

Run the script on a laptop with Wi-Fi (macOS, Linux with NetworkManager, or
Windows — standard library only, no installs). It scans for the strip, joins
its setup AP, provisions it, and rejoins your home Wi-Fi by itself:

```sh
python3 tools/provision.py --save
```

With no flags it prompts for the three things it cannot know: your home
SSID, its password (hidden), and the controller IP. `--save` remembers them
in `~/.config/tonly-mttl-w01/provision.conf` (mode 600) so the next strip is
a single command. Flags beat the file; the file beats a prompt:

```sh
python3 tools/provision.py --ssid "YourWiFi" --password "yourpassword" \
  --controller 192.168.1.50 --save
```

`--controller` is the Home Assistant host's LAN address, not the add-on's
internal hostname — the strip dials in from outside. The prompt suggests
`homeassistant.local` when it resolves. Then watch the strip appear **from
the HA host** (port 8099 is internal-only and refuses laptops on the LAN):

```sh
curl -s http://192.168.1.50:8099/api/devices | python3 -m json.tool
```

Manual overrides for odd cases: `--ap TONLY_TAP_xxxx` skips the scan,
`--gateway` overrides the strip IP, `--no-rejoin` stays on the setup AP,
`--no-verify` skips the controller check, `--reboot` also sends
`up:reboot:0` (normally omitted — the strip reboots itself).

Per-OS notes: newer macOS removed the `airport` scanner, so there the strip
AP name comes from `--ap` (joining still works; may need `sudo`). Linux
needs `nmcli`. Windows joins via a temporary WPA2 profile that is deleted
afterwards. Many phones will not show nearby network names unless
**Location services** are on — an empty scan means the system would not tell
us, not that nothing is there.

## Two warnings

**Do not pair the strip with the vendor app first.** That points it at the
vendor's cloud instead of your controller, and you then have to provision it
again.

**Unplug anything from a spare strip before provisioning it.** Its relays can
open during setup and during the reboot that follows. If you are adding a second
strip to a house where a server is running, do this when someone is there.

## Where the numbers live

`--controller` is the address of the box running Home Assistant, on your LAN.
The add-on publishes only TCP 10086 to the LAN, so if the strip does not appear
after rebooting, check that the host firewall is not blocking 10086 inbound.