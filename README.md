# WASTIFY KIOSK STANDARD — 1.0.0-rc.1

**Version 1.0.0-rc.1 — release candidate; not field certified.**

One kiosk package for Raspberry Pi 5 and Compute Module 4 units, including
Waveshare carrier boards, running **Raspberry Pi OS Trixie Desktop 64-bit with
a working Wayland desktop**. Use the same desktop account for installation and
every management command.

Prepare the screen, touch, camera, scale, any board-specific drivers and
Raspberry Pi Connect separately. Enable Desktop Autologin and turn screen
blanking off in Raspberry Pi configuration. The physical display must already
work before installing this package.

## Install through Raspberry Pi Connect

### 1. Prepare the new unit

Use Raspberry Pi OS **Trixie Desktop 64-bit**, with the screen and touch already
working. A Waveshare carrier or display may need its own hardware setup first.
Use the same normal desktop user for Connect and the kiosk.

If the unit is not linked to Connect yet, run these in its desktop terminal:

```bash
rpi-connect on
rpi-connect signin
```

Open the verification link printed by the second command on your computer and
finish signing in. Then open [Raspberry Pi Connect](https://connect.raspberrypi.com),
choose the unit and select **Connect via → Remote shell**.

### 2. Set automatic desktop startup

In the Connect remote shell, run:

```bash
sudo raspi-config
```

Set these options, using the menu labels shown by your installed version:

- **System Options → Boot**: Desktop.
- **System Options → Auto Login**: Yes for desktop auto login. Console auto login
  is not needed. If your version combines these options under **Boot / Auto
  Login**, choose **Desktop Autologin**.
- **Display Options → Screen Blanking**: No.

Choose Finish and reboot when asked. If it does not offer a reboot, run:

```bash
sudo reboot
```

Reconnect to the same unit after it comes back online. Connect screen sharing
should show its desktop. These settings are documented in the
[Raspberry Pi configuration guide](https://www.raspberrypi.com/documentation/computers/configuration.html).

### 3. Paste the installer

On your computer, open **INSTALL-VIA-CONNECT.txt from the v1.0.0-rc.1 tag** in this
repository, click **Raw**, and copy the entire file. Sign into GitHub on your
computer if the repository is private. The Pi does not need a GitHub login.

Paste the entire block into **Connect → Remote shell** as the normal desktop
user, without `sudo`. When prompted for **Unit HTTPS URL**, enter this unit's
actual site URL, for example `https://touch.wastify.co.uk/YOUR_SITE_CODE` with
`YOUR_SITE_CODE` replaced. Enter your Pi user's password if dependency
installation requests sudo. The block contains this fixed release and checks
its payload before installation; no archive transfer is needed.

### 4. Check and commission

After the installer completes, run:

```bash
export PATH="$HOME/.local/bin:$PATH"
kiosk status
kiosk check
```

Expect version `1.0.0-rc.1`, your unit's URL, `autostart: True`, and an active
running service. If it reports **Current start deferred**, the browser needs a
working desktop login; check Desktop Autologin and reboot.

Open Connect screen sharing. Sign into Wastify if required, grant camera and
USB serial scale access, and test touch, a scale reading, and a saved weight/photo.
Finally run:

```bash
sudo reboot
```

Reconnect and confirm the kiosk opened automatically:

```bash
~/.local/bin/kiosk status
```

If something fails, inspect `~/.local/bin/kiosk logs`. Field-test this candidate
on one Pi 5 and one CM4 before using it across installations.

### Alternative: extracted source installation

Alternatively, transfer and extract the source archive to the Pi, then open **Connect → Remote shell**
as the desktop user. From the extracted release directory:

```bash
bash install.sh --deps 'https://touch.wastify.co.uk/YOUR_SITE_CODE'
export PATH="$HOME/.local/bin:$PATH"
kiosk status
```

Use the unit's actual HTTPS URL. Run the installer **without sudo**. `--deps`
requests sudo only to install missing Chromium and curl packages. The installer
checks for conflicting older kiosks before package changes; disable the old
kiosk and its recovery helpers explicitly if it reports them. Existing units need a deliberate migration; this installer does not replace a live legacy setup. Desktop login must be available to start
the browser.

For a staged first install, add `--no-start`, then use `kiosk enable` when ready.
The default starts a first installation automatically. Reinstalling or updating
always preserves existing running and autostart settings, with or without
`--no-start`; updating a running kiosk still restarts its browser. Use
`kiosk enable` explicitly to enable an existing disabled installation.

The Connect paste block and the example above make `kiosk` available in that
shell. The next desktop login or reboot normally adds `~/.local/bin` to PATH.
A new terminal opened within the existing desktop may still need
`export PATH="$HOME/.local/bin:$PATH"` before the commissioning reboot.
The full command always works: `~/.local/bin/kiosk status`.

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
