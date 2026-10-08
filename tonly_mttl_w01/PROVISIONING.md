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

## Steps

1. Find the strip's setup network. It is `TONLY_TAP_<suffix>` or
   `ONLY_TAP_<suffix>`; the passphrase is `LGU_<suffix>`. Many phones will not
   show you nearby network names unless **Location services** are on — an empty
   list means the system would not tell us, not that nothing is there.
2. Join that network from a laptop or phone.
3. Run the script, passing the address the strip should call back. **Use the
   Home Assistant host's LAN address**, not the add-on's internal one — the strip
   has to dial in from outside:

   ```sh
   python3 tools/provision.py \
     --ssid "YourWiFi" --password "yourpassword" \
     --ap TONLY_TAP_3355BA3 \
     --controller 192.168.1.50
   ```

   `--controller` is the Home Assistant machine's address on your LAN.
4. Watch the strip appear:

   ```sh
   curl -s http://192.168.1.50:8099/api/devices | python3 -m json.tool
   ```

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