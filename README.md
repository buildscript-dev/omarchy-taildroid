# Taildroid (buildscript fork)

Apple-Continuity-style Samsung/Android integration for Omarchy, forked from
[raythurman2386/taildroid](https://github.com/raythurman2386/taildroid).
The Dynamic Island loads `Service.qml`; the bar widget is optional.

## Parts

| Piece | What it does |
|---|---|
| `mirror/` → `~/.local/bin/taildroid-mirror` | Qt6 client for the scrcpy server (pinned to the system scrcpy). Galaxy frame or official Samsung emulator skin, H.265 up to 120 fps, VA-API decode on the AMD iGPU, UHID keyboard, touch, clipboard, audio via a video-less scrcpy. |
| `phoned/phoned.py` | One daemon: adb presence + Wi-Fi reconnect, BlueZ phone link, PipeWire HFP telephony (`org.pipewire.Telephony`), KDE Connect (SMS threads, battery, signal, contacts, photos, find phone). JSON lines over stdin/stdout. |
| `Service.qml` | Runs phoned, launches mirrors (phone, DeX, single app, webcam), exposes `calls`, `conversations`, `answer()`, `dial()`, `sendSms()`… |
| Island | Incoming-call pill (answer/decline), call live activity, Control Center pages: Phone, Call/Keypad, Messages, Thread. |

## Keys

| Keys | Action |
|---|---|
| Super+Shift+I | Mirror on/off |
| Super+Ctrl+M | Messages |
| Super+Ctrl+J | Keypad / current call |
| Super+Ctrl+U | Answer |
| Super+Ctrl+Escape | Hang up / decline |
| Super+Shift+Ctrl+D | Samsung DeX window (One UI 8+) |

In the mirror: left click = touch, right click = back, middle = home, wheel =
scroll, keyboard goes to the phone. Hover the window for Back/Home/Recents/
Notifications/Rotate/Screen/Close; drag the bezel to move it. Side keys on the
frame are clickable (volume, power).

## Config — `~/.config/taildroid/mirror.json`

`skin` (folder name under `~/.local/share/taildroid/skins/` or a path),
`frameColor`, `codec` (h265/h264/av1), `bitrateMbps`, `maxFps`, `maxSize`,
`screenOff` (phone panel off while mirroring), `audio`, `dexDisplay`.

Official skins: developer.samsung.com/galaxy-emulator-skin (Samsung account).
Unzip into `~/.local/share/taildroid/skins/<Model>/` (the folder with `layout`).

## Frames

`"frame"` in mirror.json: `s24` (default — Galaxy S24, 70.6 × 147 mm, rounded
corners, flat aluminum sides, real bezel ratios) or `s24-ultra` (the first
built-in frame, kept as-is). `"frameColor"`: onyx-black, marble-grey,
cobalt-violet, amber-yellow, jade-green, sandstone-orange, sapphire-blue,
titanium-black/gray/violet/yellow, or #hex. Both frames are also saved as PNG
skins in `~/.local/share/taildroid/skins/Galaxy-S24{,-Ultra}/`;
`taildroid-mirror --frame s24 --export-skin DIR` regenerates one.

## Bluetooth

Bluetooth is the "nearby" signal: when the phone links, the island says so,
adb reconnects over Wi-Fi at once, calls route through the hands-free link,
contacts sync over PBAP (bluez-obex), battery comes from BlueZ, phone music
shows in the island (mpris-proxy), and the Hotspot button tethers through the
phone (Bluetooth PAN via NetworkManager).

## Audio route

The phone pairs as an A2DP source, so the link alone would hand its media and
its call audio to this machine the moment it connects — even while the phone is
in your hand. `phoned` decides instead, and follows whoever is using the phone:

| Situation | Sound comes out of |
| --- | --- |
| Phone screen on (scrolling, watching, in a call you answered on the handset) | the phone |
| Phone screen off or mirroring | this laptop, and the island shows it |
| Call ringing | this laptop, so the island can still answer it |
| Call answered from the island or dialled from the keypad | this laptop, even if you then wake the phone |

The switch is the PipeWire card profile for the phone: `off` leaves the audio on
the handset, the card's own profile brings it here. Screen state comes from
`adb shell dumpsys deviceidle`, polled every 3s, so the phone has to be on adb
for this to follow; without adb the audio stays on the phone.

A glance at the phone does not yank the audio back — the screen has to stay dark
for `AUDIO_GRACE` (20s) before this machine takes over again.

Pin it either way from the island: `setAudioMode("pc")`, `setAudioMode("phone")`,
`setAudioMode("follow")` (the default). Current route is in `pstate.audio`.

## Check

`taildroid-doctor` lists every link and the exact fix for anything missing.

## Setup once

1. One command (packages, webcam module on boot, KDE Connect through ufw):
   ```sh
   sudo pacman -S --needed kdeconnect bluez-obex v4l2loopback-dkms && \
   echo v4l2loopback | sudo tee /etc/modules-load.d/v4l2loopback.conf && \
   echo 'options v4l2loopback exclusive_caps=1 card_label="Galaxy S24 Camera"' | sudo tee /etc/modprobe.d/v4l2loopback.conf && \
   sudo modprobe v4l2loopback && sudo ufw allow 1714:1764/udp && sudo ufw allow 1714:1764/tcp
   ```
2. Phone: Developer options → USB debugging on, Samsung **Auto Blocker off**.
   Plug in USB and accept the prompt. phoned switches adb to TCP 5555 so the
   phone keeps working over Wi-Fi after unplugging.
3. Pair the phone in Bluetooth settings and allow calls → calls ring on the PC.
4. KDE Connect app on the phone: pair, grant SMS, contacts, notifications.
5. Optional webcam: `v4l2loopback` module loaded with `exclusive_caps=1`.

## Build

```sh
cd mirror && mkdir -p build && cd build && qmake6 ../taildroid-mirror.pro && make -j
ln -sf "$PWD/taildroid-mirror" ~/.local/bin/taildroid-mirror
```

Rebuild after a scrcpy upgrade is not needed (the server is read from
`/usr/share/scrcpy` and the version from `scrcpy --version`), but the
protocol can change between scrcpy releases.
