#!/bin/bash
# taildroid-doctor: checks every link between this PC and the phone.
ok() { printf '  \e[32m✔\e[0m %s\n' "$1"; }
no() { printf '  \e[31m✘\e[0m %s\n     → %s\n' "$1" "$2"; }
echo "Taildroid doctor"
echo "PC"
[ -x ~/.local/bin/taildroid-mirror ] && ok "mirror app built" || no "mirror app missing" "cd mirror/build && qmake6 .. && make"
pgrep -f phoned/phoned.py >/dev/null && ok "phoned running" || no "phoned not running" "omarchy restart shell"
pacman -Q kdeconnect &>/dev/null && ok "KDE Connect installed" || no "KDE Connect missing (messages, battery, photos)" "sudo pacman -S kdeconnect"
pacman -Q bluez-obex &>/dev/null && ok "Bluetooth contacts (bluez-obex)" || no "bluez-obex missing (caller names over Bluetooth)" "sudo pacman -S bluez-obex"
systemctl --user is-active -q mpris-proxy && ok "phone media in the island (mpris-proxy)" || no "mpris-proxy off" "systemctl --user enable --now mpris-proxy"
ls /sys/devices/virtual/video4linux &>/dev/null && ok "webcam loopback ready" || no "phone-as-webcam not set up (optional)" "see README setup command"
skin=$(python3 -c 'import json,os;print(json.load(open(os.path.expanduser("~/.config/taildroid/mirror.json"))).get("frame","s24"))' 2>/dev/null)
ok "frame: $skin (skins in ~/.local/share/taildroid/skins)"
echo "Phone"
serial=$(adb devices | awk 'NR>1 && $2=="device"{print $1; exit}')
if [ -n "$serial" ]; then
  ok "adb: $(adb -s "$serial" shell getprop ro.product.model 2>/dev/null) ($serial)"
else
  adb devices | grep -q unauthorized && no "phone unauthorized" "unlock the phone and accept the USB debugging prompt" \
    || no "phone not on adb" "Developer options → USB debugging on, Samsung Auto Blocker OFF, plug in USB"
fi
bt=$(bluetoothctl devices Paired 2>/dev/null | while read -r _ mac name; do bluetoothctl info "$mac" | grep -q 'Icon: phone' && echo "$mac $name"; done | head -1)
if [ -n "$bt" ]; then
  bluetoothctl info "${bt%% *}" | grep -q 'Connected: yes' && ok "Bluetooth: ${bt#* } connected" || no "Bluetooth: ${bt#* } paired, not connected" "turn Bluetooth on at the phone"
else
  no "phone not paired over Bluetooth (calls)" "Settings → Bluetooth on the PC, pair the phone, allow calls/contacts"
fi
busctl --user tree org.pipewire.Telephony 2>/dev/null | grep -q ag && ok "calls ready (hands-free link)" || no "no hands-free link yet" "phone Bluetooth settings → this PC → Calls on"
if busctl --user list 2>/dev/null | grep -q org.kde.kdeconnect; then
  n=$(busctl --user call org.kde.kdeconnect /modules/kdeconnect org.kde.kdeconnect.daemon devices bb false true 2>/dev/null | awk '{print $2}')
  [ "${n:-0}" -gt 0 ] && ok "KDE Connect paired" || no "KDE Connect not paired" "open KDE Connect on the phone, pair with this PC, allow SMS/contacts/notifications"
fi
