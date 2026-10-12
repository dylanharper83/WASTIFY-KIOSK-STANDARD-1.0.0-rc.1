# WASTIFY KIOSK STANDARD — 1.0.0-rc.2

**Version 1.0.0-rc.2 — release candidate; not field certified.**

One kiosk package for Raspberry Pi 5 and Compute Module 4 units, including
Waveshare carrier boards, running **Raspberry Pi OS Trixie Desktop 64-bit with
a working Wayland desktop**. Use the same desktop account for installation and
every management command.

Prepare the physical screen/touch and any board-specific drivers separately.
The standard installer automatically selects desktop boot, enables desktop
login for the installing user, disables stock screen blanking, installs the kiosk
and reboots a new unit once. There are no raspi-config menus to complete.

## Install through Raspberry Pi Connect

On a new **Trixie Desktop 64-bit** Pi 5 or CM4, open **Connect → Remote shell**
as the normal desktop user and paste this one line, without sudo:

```bash
bash -o pipefail -c 'curl -fsSL https://github.com/dylanharper83/WASTIFY-KIOSK-STANDARD-1.0.0-rc.1/raw/main/go | bash'
```

Save this same line for every new unit; it is also in `INSTALL-COMMAND.txt`.
The small `go` launcher downloads the complete **v1.0.0-rc.2** installer and
verifies its SHA256 before running it. A failed or incomplete installer download
is never executed. The command needs curl, which is included in the standard
Raspberry Pi OS Desktop image. It does not ask for a site URL or need GitHub
authentication on the Pi.

Enter the Pi user's admin password if requested. New units configure themselves
and reboot automatically after successful installation. Reconnect to the same
unit after it returns online. The kiosk displays **Waiting for site setup** until
you choose a URL. Failed installation does not request a reboot.

The launcher currently selects rc.2. Future approved releases can update the
launcher while the saved command stays the same. Existing kiosks do not update
automatically. `ONE-COMMAND.txt` remains the longer command pinned to rc.2;
the complete `INSTALL-VIA-CONNECT.txt` can also be copied and pasted directly.

## Set or change the URL later

After installation, run this in Connect or a local terminal, replacing the
example with the unit's actual HTTPS site URL:

```bash
kiosk url 'https://touch.wastify.co.uk/YOUR_SITE_CODE'
```

A running kiosk opens the new site immediately. A stopped kiosk stays stopped;
use `kiosk start` when ready. Camera and USB serial scale permissions must be
granted through Connect screen sharing on the site's first use. Check a live
scale reading, saved weight/photo and physical screen/touch before site use.

## Alternative: extracted source installation

From the extracted release directory, the same automatic fresh-unit setup is:

```bash
bash install.sh --deps --setup-desktop
export PATH="$HOME/.local/bin:$PATH"
```

This source command does not reboot automatically; reboot when commissioning
to apply desktop settings. The Connect paste installer performs that reboot on
new units. Omitting `--setup-desktop` leaves desktop settings alone.

Reinstalling or updating preserves the current URL, running state and desktop
startup setting. The `--setup-desktop` flag only acts when the kiosk runtime is
absent. A first install can omit the URL; `null` means unconfigured, while empty
or invalid URLs are rejected. Use `--no-start` to stage a first installation.

The Connect paste block exports `~/.local/bin` into that shell. After its reboot,
new Connect shells normally include this directory. The full path always works:
`~/.local/bin/kiosk status`.

## On-site and remote commands

```bash
kiosk help
kiosk status
kiosk check
kiosk support
kiosk version
kiosk start
kiosk stop
kiosk restart
kiosk enable
kiosk disable
kiosk url 'https://touch.wastify.co.uk/OTHER_SITE_CODE'
kiosk logs
kiosk wifi
kiosk update '/home/YOUR_USER/wastify-kiosk-RELEASE'
kiosk rollback
```

`start`, `stop` and `restart` affect the current session. `enable` starts now and
at desktop login; `disable` stops now and disables desktop autostart. Changing
the URL restarts only a running kiosk. Finish any weighing or upload before a
restart, update or rollback. `wastify-kiosk` remains an alias for the same tool.

`check` reports local diagnostics; `support` saves a redacted diagnostic file.
`wifi` opens the operating system's text menu in the Connect shell or a local
terminal, with a timed network rollback if the change is not confirmed. For
touchscreen Wi-Fi setup, run `kiosk stop` and use the Pi desktop network menu,
then `kiosk start`.

## Commission, update and recover

Through Connect screen sharing, sign in to Wastify and grant camera and USB
serial scale permission on the dedicated kiosk profile's first use. Verify
the physical screen and touch, a live scale reading, a saved weight/photo,
restart and reboot. The browser profile persists across installs and restarts;
an existing browser's permissions are not automatically migrated.

To update, transfer and extract a tested release, then run
`kiosk update '/path/to/extracted-release'`. You can also run `bash install.sh`
from that directory; omitting the URL keeps the installed URL. Each installation
backs up the managed files. `kiosk rollback` restores the previous code and
leaves the kiosk stopped; inspect it, then run `kiosk start`. The current site
URL and browser data are retained.
Rollback requires a previous installed release and refuses the empty snapshot
created before the first installation.

The desktop starts one Chromium instance supervised by systemd. Three starts within five minutes exhaust the crash retry budget; inspect the logs and use `kiosk restart` to reset it. This prevents continuous crash loops. The service recovers from
browser exits within that budget; the PWA handles offline operation. There is no automatic browser
restart after an internet outage. An uncached first load may require
`kiosk restart` after internet returns. Service status alone cannot prove that
the physical display, website, camera or scale is working.

Run the spare-unit field checks before adopting this release across sites.
Install future tested releases through the installer; keep the deployed files
unchanged on individual units.

Installation and network-menu references: [Raspberry Pi Connect](https://www.raspberrypi.com/documentation/services/connect.html), [NetworkManager checkpoint](https://networkmanager.pages.freedesktop.org/NetworkManager/NetworkManager/nmcli.html).
