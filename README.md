# iphone-notify

Mirror iPhone notifications to the GNOME desktop on Linux over Bluetooth, with a
Quick Settings tile. Nothing needs to be installed on the iPhone.

It uses Apple's [Notification Center Service (ANCS)][ancs] over Bluetooth Low
Energy — the same mechanism smartwatches use. The computer advertises itself as
an ANCS accessory, the iPhone pairs with it, and each notification on the phone
becomes a desktop notification. Clearing it on the phone clears it on the desktop.

## Parts

- **`iphone_notify.py`** — daemon (systemd user service). Talks to BlueZ over
  D-Bus, handles pairing, subscribes to ANCS, posts desktop notifications, and
  exposes `io.github.arjun.iPhoneNotify` on the session bus.
- **`extension/`** — GNOME Shell extension (GNOME 50/51). A Quick Settings tile
  to turn mirroring on/off, pair an iPhone and see recent notifications, plus a
  top-bar icon while the phone is connected. Bluetooth work stays in the daemon
  so a Bluetooth hiccup can't freeze the shell.

## Requirements

- Linux with BlueZ 5.x and a Bluetooth LE adapter
- `python3-dbus` and `python3-gobject` (installed by `install.sh` on Fedora)
- GNOME Shell 50 or 51 for the extension (the daemon works without it)

## Install

```sh
./install.sh
```

Then log out and back in so GNOME Shell loads the extension.

## Pair your iPhone

1. Open pairing: Quick Settings › iPhone › **Pair new iPhone…**, or run
   `iphone-notify pair`. The window stays open for 3 minutes.
2. On the iPhone: Settings › Bluetooth, tap this computer under Other Devices.
3. Tap **Pair** when the codes match (the code is also shown on the desktop).
4. Tap **Allow** when iOS asks whether to share notifications.

**Already paired the iPhone over classic Bluetooth (e.g. via GNOME Settings)?**
iOS only shares notifications with accessories paired over LE, and in that case
won't show a *Share System Notifications* switch. Forget the computer on the
iPhone, remove the iPhone on the computer (`bluetoothctl remove <address>`),
then pair again as above. Pairing mode advertises over LE only so the phone
can't pick classic Bluetooth again.

## Usage

```sh
journalctl --user -u iphone-notify -f     # daemon logs
iphone-notify -v                          # run in the foreground, verbose
iphone-notify --show-existing             # also show notifications already on the phone
systemctl --user disable --now iphone-notify   # turn off (the tile does this too)
```

## Limitations

- Bluetooth range (~10 m).
- Read-only: replying or triggering notification actions isn't implemented.
- Notifications already on the phone when it connects are skipped unless
  `--show-existing` is given.

[ancs]: https://developer.apple.com/library/archive/documentation/CoreBluetooth/Reference/AppleNotificationCenterServiceSpecification/Introduction/Introduction.html
