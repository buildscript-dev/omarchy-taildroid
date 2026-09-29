#!/usr/bin/env python3
"""phoned — one process that knows everything about the phone.

Sources:
  adb            presence, model, USB/Wi-Fi transport, automatic Wi-Fi reconnect
  BlueZ          the phone's Bluetooth link (needed for calls), auto-connect
  PipeWire       Bluetooth hands-free telephony (org.pipewire.Telephony)
  KDE Connect    battery, signal, SMS threads, notifications, files, find phone

Protocol: JSON lines. stdout gets {"type":"state",...} snapshots and
{"type":"event",...} one-shots; stdin takes {"cmd":...} lines.
"""

from __future__ import annotations

import base64
import fcntl
import glob
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
session = dbus.SessionBus()
system = dbus.SystemBus()

TEL = "org.pipewire.Telephony"
KDEC = "org.kde.kdeconnect"
KDEC_ROOT = "/modules/kdeconnect"
STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "taildroid")
os.makedirs(STATE_DIR, exist_ok=True)
MEMORY = os.path.join(STATE_DIR, "phone.json")
CHATS = os.path.join(STATE_DIR, "chats.json")
# KDE Connect drops phone app icons and MMS pictures in /tmp, which a reboot wipes.
SHARE_DIR = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "taildroid")
ICON_DIR = os.path.join(SHARE_DIR, "appicons")
THUMB_DIR = os.path.join(SHARE_DIR, "attachments")
FACE_DIR = os.path.join(SHARE_DIR, "contacts")
for _d in (ICON_DIR, THUMB_DIR, FACE_DIR):
    os.makedirs(_d, exist_ok=True)

state = {
    "phone": {"serial": "", "model": "", "transport": "none", "wifiAddress": ""},
    "battery": {"level": -1, "charging": False},
    "signal": {"network": "", "strength": -1},
    "bluetooth": {"address": "", "name": "", "paired": False, "connected": False},
    "hfp": {"ready": False, "path": ""},
    "calls": [],
    "kdeconnect": {"running": False, "deviceId": "", "name": "", "reachable": False, "paired": False},
    "conversations": [],
    "mounted": "",
    "hotspot": False,
    "audio": {"route": "phone", "mode": "follow", "screenOn": True},
    "phoneApps": {},
    "phoneChats": {},
    "contactFaces": {},
    "phoneNotifs": [],
    "chats": [],
}
calls: dict[str, dict] = {}
threads: dict[int, dict] = {}
messages: dict[int, dict] = {}  # threadId -> {uid: message}
open_thread = {"id": 0}
open_chat = {"key": ""}
contacts: dict[str, str] = {}
contact_faces: dict[str, str] = {}  # last 9 digits -> that contact's photo
phone_apps: dict[str, str] = {}  # Android app name -> kept copy of its launcher icon
attachment_files: dict[str, str] = {}  # attachment uniqueIdentifier -> local file
phone_notifs: dict[str, dict] = {}  # KDE Connect notification id -> what the phone shows
chat_icons: dict[str, str] = {}  # "<app>\x00<chat title>" -> kept copy of that chat's picture
# Apps that only reach this machine as notifications (WhatsApp, Signal, …) still
# make a conversation once their lines are kept in order instead of replaced.
chat_log: dict[str, dict] = {}
memory = {}
try:
    with open(MEMORY) as f:
        memory = json.load(f)
except (OSError, ValueError):
    pass


def write_json(path, obj):
    """Write-then-rename: a kill mid-write must not leave a torn file behind."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def remember(**kw):
    memory.update(kw)
    write_json(MEMORY, memory)


# ------------------------------------------------------------------ output
_flush_pending = False


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def changed():
    global _flush_pending
    if _flush_pending:
        return

    def flush():
        global _flush_pending
        _flush_pending = False
        state["calls"] = sorted(calls.values(), key=lambda c: c["since"])
        state["conversations"] = sorted(threads.values(), key=lambda t: -t["date"])[:250]
        emit({"type": "state", **state})
        return False

    _flush_pending = True
    GLib.timeout_add(40, flush)


def event(kind, **kw):
    emit({"type": "event", "kind": kind, **kw})


def run(*cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def to_py(v):
    if isinstance(v, (dbus.String, dbus.ObjectPath)):
        return str(v)
    if isinstance(v, dbus.Boolean):
        return bool(v)
    if isinstance(v, (dbus.Int16, dbus.Int32, dbus.Int64, dbus.UInt16, dbus.UInt32, dbus.UInt64, dbus.Byte)):
        return int(v)
    if isinstance(v, (dbus.Array, list, dbus.Struct, tuple)):
        return [to_py(x) for x in v]
    if isinstance(v, dbus.Dictionary):
        return {str(k): to_py(x) for k, x in v.items()}
    return v


# ------------------------------------------------------------------ contacts
def digits(number: str) -> str:
    d = re.sub(r"\D", "", number or "")
    return d[-9:]


def save_face(key, params, payload):
    """vCards carry the contact's picture inline. Write it out once so the island
    can show a face next to a conversation instead of a speech bubble."""
    raw = re.sub(r"\s+", "", payload or "")
    if not raw:
        return ""
    ext = ".png" if "PNG" in params.upper() else ".jpg"
    path = os.path.join(FACE_DIR, key + ext)
    if not os.path.exists(path):
        try:
            with open(path, "wb") as f:
                f.write(base64.b64decode(raw))
        except (OSError, ValueError):
            return ""
    return path


def load_contacts():
    contacts.clear()
    contact_faces.clear()
    base = os.path.expanduser("~/.local/share/kpeoplevcard")
    paths = glob.glob(os.path.join(base, "kdeconnect-*", "*.vcf"))
    paths += [os.path.join(STATE_DIR, "phonebook.vcf")]
    for path in paths:
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for card in text.split("BEGIN:VCARD"):
            m = re.search(r"^FN[^:]*:(.+)$", card, re.M)
            name = m.group(1).strip() if m else ""
            # vCard 2.1 folds the photo over the following indented lines.
            photo = re.search(r"^PHOTO([^:]*):(.*(?:\n[ \t].*)*)", card, re.M)
            for tel in re.findall(r"^TEL[^:]*:(.+)$", card, re.M):
                key = digits(tel)
                if name and key:
                    contacts[key] = name
                    if photo and key not in contact_faces:
                        face = save_face(key, photo.group(1), photo.group(2))
                        if face:
                            contact_faces[key] = face
    state["contactFaces"] = dict(contact_faces)


def contact_name(number: str) -> str:
    return contacts.get(digits(number), "")


def contact_face(number: str) -> str:
    return contact_faces.get(digits(number), "")


# ------------------------------------------------------------------ adb
def adb_props(serial):
    model = run("adb", "-s", serial, "shell", "getprop ro.product.model") or serial
    state["phone"]["model"] = model
    wifi = run("adb", "-s", serial, "shell", "ip -f inet addr show wlan0")
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", wifi)
    if m:
        state["phone"]["wifiAddress"] = m.group(1)
        mac = neighbor_mac(m.group(1))
        remember(wifiAddress=m.group(1), model=model, **({"wifiMac": mac} if mac else {}))
    battery = run("adb", "-s", serial, "shell", "dumpsys battery")
    lvl = re.search(r"level: (\d+)", battery)
    if lvl and state["battery"]["level"] < 0:
        state["battery"]["level"] = int(lvl.group(1))
        state["battery"]["charging"] = bool(re.search(r"(AC|USB) powered: true", battery))
    # Keep adbd listening on TCP so the phone stays reachable once unplugged.
    if ":" not in serial and run("adb", "-s", serial, "shell", "getprop service.adb.tcp.port") != "5555":
        run("adb", "-s", serial, "tcpip", "5555")
    changed()


def radio_name(types: str) -> str:
    """First cellular radio in gsm.network.type ("LTE,IWLAN"), named like KDE Connect names it."""
    for t in types.split(","):
        t = t.strip().upper()
        if t and t not in ("IWLAN", "UNKNOWN"):
            return "5G" if t.startswith("NR") else t
    return ""


def set_signal(network, strength):
    # KDE Connect reads the data SIM, and says "Unknown" when that SIM rides
    # Wi-Fi calling (IWLAN). Ask adb which radio the SIMs are really on.
    serial = state["phone"]["serial"]
    if network == "Unknown" and serial:
        network = radio_name(run("adb", "-s", serial, "shell", "getprop gsm.network.type", timeout=4)) or network
    state["signal"] = {"network": network, "strength": strength}


def parse_devices(block: str):
    devs = []
    for line in block.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devs.append(parts[0])
    return devs


_track_buf = b""


def on_track(fd, cond):
    global _track_buf
    chunk = os.read(fd, 65536)
    if not chunk:
        GLib.timeout_add_seconds(3, start_track)
        return False
    _track_buf += chunk
    while len(_track_buf) >= 4:
        n = int(_track_buf[:4], 16)
        if len(_track_buf) < 4 + n:
            break
        block = _track_buf[4:4 + n].decode(errors="replace")
        _track_buf = _track_buf[4 + n:]
        devs = parse_devices(block)
        usb = [d for d in devs if ":" not in d and not d.startswith("adb-")]
        serial = (usb or devs or [""])[0]
        before = state["phone"]["serial"]
        state["phone"]["serial"] = serial
        state["phone"]["transport"] = "none" if not serial else ("usb" if serial in usb else "wifi")
        if serial and serial != before:
            state["phone"]["model"] = memory.get("model", "")
            GLib.idle_add(lambda s=serial: (adb_props(s), False)[1])
            event("connected", transport=state["phone"]["transport"], model=state["phone"]["model"])
        elif not serial and before:
            event("disconnected")
        changed()
    return True


def start_track():
    proc = subprocess.Popen(["adb", "track-devices"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    GLib.io_add_watch(proc.stdout.fileno(), GLib.IO_IN | GLib.IO_HUP, on_track)
    return False


def neighbor_mac(ip):
    m = re.search(r"lladdr (\S+)", run("ip", "neigh", "show", ip))
    return m.group(1) if m else ""


def neighbor_ip(mac, table):
    """The phone's current address on the LAN, found by its Wi-Fi MAC."""
    for line in table.splitlines():
        parts = line.split()
        if mac and f"lladdr {mac}" in line and re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", parts[0]):
            return parts[0]
    return ""


def wifi_reconnect():
    # DHCP can hand the phone a new address; its MAC (per network) stays the same.
    addr = neighbor_ip(memory.get("wifiMac", ""), run("ip", "-4", "neigh")) or memory.get("wifiAddress")
    if addr and state["phone"]["transport"] == "none":
        subprocess.Popen(["adb", "connect", f"{addr}:5555"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


# ------------------------------------------------------------------ bluetooth
BT_RETRY_MIN, BT_RETRY_MAX = 60, 600
bt_retry = {"at": 0.0, "wait": BT_RETRY_MIN}


def bt_scan():
    try:
        om = dbus.Interface(system.get_object("org.bluez", "/"), "org.freedesktop.DBus.ObjectManager")
        objects = om.GetManagedObjects()
    except dbus.DBusException:
        return True
    best = None
    for path, ifaces in objects.items():
        dev = ifaces.get("org.bluez.Device1")
        if not dev or not dev.get("Paired"):
            continue
        if str(dev.get("Icon", "")) == "phone" or str(dev.get("Address")) == memory.get("btAddress"):
            best = (path, dev)
            if dev.get("Connected"):
                break
    if best:
        path, dev = best
        bt = state["bluetooth"]
        new = {"address": str(dev["Address"]), "name": str(dev.get("Alias", dev.get("Name", ""))),
               "paired": True, "connected": bool(dev.get("Connected")), "path": str(path)}
        battery = objects.get(path, {}).get("org.bluez.Battery1")
        if battery and not state["kdeconnect"]["reachable"]:
            state["battery"]["level"] = int(battery.get("Percentage", -1))
        if new != bt:
            arrived = new["connected"] and not bt.get("connected")
            state["bluetooth"] = new
            remember(btAddress=new["address"])
            if arrived:
                # Bluetooth is the first sign the phone is near: announce it,
                # bring adb back over Wi-Fi right away, refresh caller names.
                event("nearby", name=new["name"])
                wifi_reconnect()
                GLib.timeout_add(500, lambda: (audio_now(), False)[1])
                GLib.timeout_add_seconds(5, lambda: (pull_phonebook(new["address"]), False)[1])
            elif bt.get("connected") and not new["connected"]:
                event("away", name=new["name"])
            changed()
        # The phone is around (adb sees it) but its Bluetooth link dropped.
        # Back off: a Connect sent while BlueZ is still paging answers
        # br-connection-busy, and a steady stream of them kept that device
        # "busy" until it was unpaired.
        if new["connected"]:
            bt_retry.update(at=0, wait=BT_RETRY_MIN)
        elif state["phone"]["serial"] and time.time() >= bt_retry["at"]:
            bt_retry["at"] = time.time() + bt_retry["wait"]
            bt_retry["wait"] = min(bt_retry["wait"] * 2, BT_RETRY_MAX)
            try:
                dbus.Interface(system.get_object("org.bluez", path), "org.bluez.Device1").Connect(
                    reply_handler=lambda: None, error_handler=lambda e: None, timeout=60)
            except dbus.DBusException:
                pass
    return True


def pull_phonebook(address):
    """Contacts over Bluetooth (PBAP, needs bluez-obex): caller names without KDE Connect."""
    dest = os.path.join(STATE_DIR, "phonebook.vcf")
    if os.path.exists(dest) and time.time() - os.path.getmtime(dest) < 12 * 3600:
        return
    try:
        obex = dbus.SessionBus()
        client = dbus.Interface(obex.get_object("org.bluez.obex", "/org/bluez/obex"), "org.bluez.obex.Client1")
        session_path = client.CreateSession(address, {"Target": "PBAP"})
        pbap = dbus.Interface(obex.get_object("org.bluez.obex", session_path), "org.bluez.obex.PhonebookAccess1")
        pbap.Select("int", "pb")
        transfer, _ = pbap.PullAll(dest, {"Format": "vcard30", "Fields": dbus.Array(["FN", "TEL"], signature="s")})

        def done(iface, props, inv):
            if props.get("Status") in ("complete", "error"):
                load_contacts()
                try:
                    client.RemoveSession(session_path)
                except dbus.DBusException:
                    pass
        obex.add_signal_receiver(done, "PropertiesChanged", "org.freedesktop.DBus.Properties", "org.bluez.obex", transfer)
    except dbus.DBusException:
        pass  # obexd missing or the phone refused phonebook access


HOTSPOT = "Galaxy Hotspot"


def hotspot_work(on: bool):
    """Instant Hotspot: internet through the phone over Bluetooth tethering (PAN). Runs off the main loop."""
    addr = state["bluetooth"].get("address")
    if not addr:
        return "Pair the phone over Bluetooth first"
    name = HOTSPOT
    if on:
        if name not in run("nmcli", "-t", "-f", "NAME", "connection", "show").splitlines():
            run("nmcli", "connection", "add", "type", "bluetooth", "con-name", name, "bt-type", "panu",
                "bluetooth.bdaddr", addr, "connection.autoconnect", "no")
        r = run("nmcli", "--wait", "30", "connection", "up", name, timeout=40)
        if "successfully" not in r:
            return "Turn on Bluetooth tethering on the phone"
    else:
        run("nmcli", "connection", "down", name)
    return ""


def hotspot_done(err):
    if err:
        event("error", message=err)
    state["hotspot"] = HOTSPOT in run("nmcli", "-t", "-f", "NAME", "connection", "show", "--active").splitlines()
    event("hotspot", on=state["hotspot"])
    changed()


# ------------------------------------------------------------------ calls
def call_from(path, props):
    number = str(props.get("LineIdentification", "") or props.get("IncomingLine", ""))
    st = str(props.get("State", ""))
    old = calls.get(path, {})
    c = {
        "path": path,
        "number": number or old.get("number", ""),
        "name": str(props.get("Name", "")) or contact_name(number) or old.get("name", ""),
        "state": st or old.get("state", ""),
        "multiparty": bool(props.get("Multiparty", old.get("multiparty", False))),
        "since": old.get("since", time.time()),
        "activeSince": old.get("activeSince", 0),
        "incoming": old.get("incoming", st in ("incoming", "waiting")),
        "onPhone": old.get("onPhone", False),
    }
    if c["state"] == "active" and not c["activeSince"]:
        c["activeSince"] = time.time()
        # Nobody here pressed answer, so the call was picked up on the handset.
        c["onPhone"] = path not in answered_here and time.time() > audio["dialUntil"]
    return c


def tel_scan():
    try:
        om = dbus.Interface(session.get_object(TEL, "/org/pipewire/Telephony"), "org.freedesktop.DBus.ObjectManager")
        objects = om.GetManagedObjects()
    except dbus.DBusException:
        state["hfp"] = {"ready": False, "path": ""}
        calls.clear()
        changed()
        return
    ag = ""
    seen = set()
    for path, ifaces in objects.items():
        if f"{TEL}.AudioGateway1" in ifaces:
            ag = str(path)
        if f"{TEL}.Call1" in ifaces:
            seen.add(str(path))
            calls[str(path)] = call_from(str(path), ifaces[f"{TEL}.Call1"])
    for p in list(calls):
        if p not in seen:
            del calls[p]
    state["hfp"] = {"ready": bool(ag), "path": ag}
    changed()


def on_tel_added(path, ifaces):
    path = str(path)
    if f"{TEL}.AudioGateway1" in ifaces:
        state["hfp"] = {"ready": True, "path": path}
    if f"{TEL}.Call1" in ifaces:
        c = call_from(path, ifaces[f"{TEL}.Call1"])
        calls[path] = c
        event("call", **c)
        audio_now()
    changed()


def on_tel_removed(path, ifaces):
    path = str(path)
    if f"{TEL}.Call1" in ifaces and path in calls:
        ended = calls.pop(path)
        answered_here.discard(path)
        event("callEnded", **ended)
        audio_now()
    if f"{TEL}.AudioGateway1" in ifaces and state["hfp"]["path"] == path:
        state["hfp"] = {"ready": False, "path": ""}
    changed()


def on_tel_props(iface, changed_props, invalidated, path=None):
    if iface == f"{TEL}.Call1" and path in calls:
        was = calls[path]["state"]
        calls[path] = call_from(path, {**changed_props})
        if calls[path]["state"] != was:
            audio_now()
        changed()
    elif iface == f"{TEL}.AudioGatewayTransport1" and "RejectSCO" in changed_props:
        # PipeWire refuses one SCO attempt, then clears RejectSCO, and the phone
        # simply tries again. Re-arm at once so every retry is refused too.
        if not bool(changed_props["RejectSCO"]) and not call_here():
            set_reject_sco(True)


def tel_call(path, method, *args, iface="Call1"):
    try:
        obj = session.get_object(TEL, path)
        getattr(dbus.Interface(obj, f"{TEL}.{iface}"), method)(*args)
        return True
    except dbus.DBusException as e:
        event("error", message=f"{method}: {e.get_dbus_message()}")
        return False


# ------------------------------------------------------------------ kde connect
def kdec(path="", iface="org.kde.kdeconnect.daemon"):
    return dbus.Interface(session.get_object(KDEC, KDEC_ROOT + path), iface)


def kdec_prop(path, iface, name, default=None):
    try:
        props = dbus.Interface(session.get_object(KDEC, KDEC_ROOT + path), "org.freedesktop.DBus.Properties")
        return to_py(props.Get(iface, name))
    except dbus.DBusException:
        return default


def dev_path(sub=""):
    return f"/devices/{state['kdeconnect']['deviceId']}{sub}"


def save_thumb(uid, b64):
    """A preview of the picture rides along with the message. Keep it on disk so
    the island shows the picture at once instead of a paperclip, while the
    full-size file is still being fetched from the phone."""
    if not uid or not b64:
        return ""
    path = os.path.join(THUMB_DIR, re.sub(r"[^A-Za-z0-9_.-]", "_", uid) + ".png")
    if not os.path.exists(path):
        try:
            with open(path, "wb") as f:
                f.write(base64.b64decode(b64))
        except (OSError, ValueError):
            return ""
    return path


def parse_attachments(raw):
    """KDE Connect sends (partID, mimeType, base64 preview, uniqueIdentifier)."""
    out = []
    for a in raw or []:
        a = to_py(a)
        if isinstance(a, list) and len(a) >= 4:
            uid = str(a[3])
            out.append({"partId": int(a[0]), "mime": str(a[1]), "uid": uid,
                        "thumb": save_thumb(uid, str(a[2]))})
    return out


def message_to_thread(msg):
    # (event, body, addresses a(s), date, type, read, threadID, uID, subID, attachments)
    m = to_py(msg)
    addresses = [a[0] if isinstance(a, list) else a for a in m[2]]
    number = addresses[0] if addresses else ""
    t = {
        "threadId": int(m[6]),
        "addresses": addresses,
        "name": contact_name(number) or number,
        "face": contact_face(number),
        "body": m[1],
        "date": int(m[3]),
        "outgoing": int(m[4]) == 2,
        "read": bool(m[5]),
        "attachments": parse_attachments(m[9] if len(m) > 9 else []),
        "uid": int(m[7]),
    }
    return t


def emit_thread():
    tid = open_thread["id"]
    if tid:
        msgs = sorted(messages.get(tid, {}).values(), key=lambda m: m["date"])[-400:]
        for m in msgs:
            m["files"] = [full_or_thumb(a) for a in m["attachments"]]
        emit({"type": "thread", "threadId": tid, "messages": msgs})
    return False


def full_or_thumb(a):
    """The full-size file once the phone has sent it, the inline preview until then."""
    full = attachment_files.get(a["uid"], "")
    return full if full and os.path.exists(full) else a["thumb"]


def want_attachments(tid):
    """Pull every picture in the open thread, so the island shows them instead of a clip."""
    kd = state["kdeconnect"]["deviceId"]
    if not kd:
        return
    conv = kdec(f"/devices/{kd}", "org.kde.kdeconnect.device.conversations")
    for m in messages.get(tid, {}).values():
        for a in m["attachments"]:
            if a["uid"] and a["uid"] not in attachment_files:
                try:
                    conv.requestAttachmentFile(dbus.Int64(a["partId"]), a["uid"])
                except dbus.DBusException:
                    pass


def on_attachment(path, name):
    attachment_files[str(name)] = str(path)
    GLib.timeout_add(60, emit_thread)


def prune_messages(tid, per_thread=400, keep_threads=250):
    """Cap the cache, not just the view: the newest threads and their newest messages."""
    msgs = messages.get(tid, {})
    if len(msgs) > per_thread:
        for uid in sorted(msgs, key=lambda u: msgs[u]["date"])[:len(msgs) - per_thread]:
            del msgs[uid]
    if len(threads) > keep_threads:
        for old in sorted(threads, key=lambda k: -threads[k]["date"])[keep_threads:]:
            if old != open_thread["id"]:
                threads.pop(old, None)
                messages.pop(old, None)
        kept = {a["uid"] for ms in messages.values() for m in ms.values() for a in m["attachments"]}
        for uid in [u for u in attachment_files if u not in kept]:
            del attachment_files[uid]


def on_conversation(msg):
    try:
        t = message_to_thread(msg)
    except (IndexError, TypeError, ValueError):
        return
    messages.setdefault(t["threadId"], {})[t["uid"]] = t
    old = threads.get(t["threadId"])
    if not old or t["date"] >= old["date"]:
        threads[t["threadId"]] = t
    prune_messages(t["threadId"])
    if t["threadId"] == open_thread["id"]:
        GLib.timeout_add(60, emit_thread)
        GLib.timeout_add(80, lambda: (want_attachments(t["threadId"]), False)[1])
    changed()


# What the phone shares with this PC through KDE Connect, each one a toggle in the island.
SHARE_PLUGINS = ("notifications", "sms", "clipboard", "contacts", "telephony", "share", "mpriscontrol", "findmyphone")


def kdec_scan():
    k = state["kdeconnect"]
    try:
        ids = [str(i) for i in kdec().devices(False, True)]
    except dbus.DBusException:
        if k["running"]:
            state["kdeconnect"] = {"running": False, "deviceId": "", "name": "", "reachable": False, "paired": False}
            changed()
        return True
    k["running"] = True
    dev = ids[0] if ids else ""
    for i in ids:  # prefer a reachable one
        if kdec_prop(f"/devices/{i}", "org.kde.kdeconnect.device", "isReachable", False):
            dev = i
            break
    if dev != k["deviceId"]:
        k["deviceId"] = dev
        threads.clear()
        if dev:
            load_contacts()
            try:
                kdec(f"/devices/{dev}", "org.kde.kdeconnect.device.conversations").requestAllConversationThreads()
                kdec(f"/devices/{dev}/contacts", "org.kde.kdeconnect.device.contacts").synchronizeRemoteWithLocal()
                for m in kdec(f"/devices/{dev}", "org.kde.kdeconnect.device.conversations").activeConversations():
                    on_conversation(m)
                scan_notifs()
            except dbus.DBusException:
                pass
    if dev:
        base = f"/devices/{dev}"
        k["name"] = kdec_prop(base, "org.kde.kdeconnect.device", "name", "")
        k["reachable"] = kdec_prop(base, "org.kde.kdeconnect.device", "isReachable", False)
        k["paired"] = kdec_prop(base, "org.kde.kdeconnect.device", "isPaired", False)
        try:
            d = kdec(base, "org.kde.kdeconnect.device")
            k["sharing"] = {p: bool(d.isPluginEnabled("kdeconnect_" + p)) for p in SHARE_PLUGINS}
        except dbus.DBusException:
            pass
        lvl = kdec_prop(base + "/battery", "org.kde.kdeconnect.device.battery", "charge", -1)
        if lvl is not None and lvl >= 0:
            state["battery"] = {"level": lvl, "charging": kdec_prop(base + "/battery", "org.kde.kdeconnect.device.battery", "isCharging", False)}
        net = kdec_prop(base + "/connectivity_report", "org.kde.kdeconnect.device.connectivity_report", "cellularNetworkType", "")
        if net:
            set_signal(net, kdec_prop(base + "/connectivity_report", "org.kde.kdeconnect.device.connectivity_report", "cellularNetworkStrength", -1))
    changed()
    return True


def on_kdec_signal(*args, **kw):
    member = kw.get("member")
    path = kw.get("path", "")
    if member in ("conversationCreated", "conversationUpdated"):
        t = message_to_thread(args[0])
        fresh = t["uid"] not in messages.get(t["threadId"], {})
        on_conversation(args[0])
        if fresh and not t["outgoing"] and time.time() * 1000 - t["date"] < 120000:
            event("sms", **t)
    elif member == "conversationRemoved":
        threads.pop(int(args[0]), None)
        changed()
    elif member == "refreshed" and path.endswith("/battery"):
        state["battery"] = {"level": int(args[1]), "charging": bool(args[0])}
        changed()
    elif member == "refreshed" and path.endswith("/connectivity_report"):
        set_signal(str(args[0]), int(args[1]))
        changed()
    elif member == "notificationRemoved":
        drop_notif(str(args[0]))
    elif member == "allNotificationsRemoved":
        phone_notifs.clear()
        publish_notifs()
    elif member == "attachmentReceived":
        on_attachment(args[0], args[1])
    elif member in ("notificationPosted", "notificationUpdated"):
        on_notif_posted(str(args[0]))
    elif member == "callReceived":
        # KDE Connect sees calls even without Bluetooth; used for caller names.
        ev, number, name = (str(a) for a in args[:3])
        if name and digits(number):
            contacts[digits(number)] = name
        if ev == "missedCall":
            event("missedCall", number=number, name=name or contact_name(number))
    elif member in ("deviceListChanged", "reachableChanged", "pairStateChanged", "localCacheSynchronized"):
        if member == "localCacheSynchronized":
            load_contacts()
        GLib.timeout_add(300, lambda: (kdec_scan(), False)[1])


# ------------------------------------------------------------------ audio route
# The phone pairs as an A2DP source, so BlueZ hands its media and its call audio
# to this machine the moment the link comes up — even while the phone is in your
# hand. One PipeWire card profile carries both, so the route is a single switch:
# profile on = this laptop, profile off = the phone keeps its own audio.
AUDIO_GRACE = 20  # seconds the phone must stay dark before the laptop takes over

audio = {"mode": "follow", "screenOn": True, "playing": False, "route": "phone", "since": 0.0,
         "profile": "", "dialUntil": 0.0, "btAt": 0.0}
answered_here = set()
_audio_lock = threading.Lock()


def bt_card():
    addr = state["bluetooth"].get("address", "")
    if not addr:
        return ""
    card = "bluez_card." + addr.replace(":", "_")
    return card if card in run("pactl", "list", "cards", "short") else ""


def card_profiles(card):
    """(active profile, first selectable profile that is not off) for a pactl card."""
    out = run("pactl", "list", "cards")
    i = out.find("Name: " + card)
    if i < 0:
        return "", ""
    block = out[i:]
    j = block.find("\n\tName: ", 1)
    if j > 0:
        block = block[:j]
    offered = [x for x in re.findall(r"^\t\t(\S+):.*available: yes", block, re.M) if x != "off"]
    active = re.search(r"Active Profile: (\S+)", block)
    return (active.group(1) if active else ""), (offered[0] if offered else "")


def wanted_route():
    """Where the phone's audio belongs right now: "pc" or "phone"."""
    if audio["mode"] != "follow":
        return audio["mode"]
    for c in calls.values():
        if c["state"] in ("incoming", "waiting", "dialing", "alerting"):
            return "pc"  # keep the link up so the island can still answer
    for c in calls.values():
        if c["state"] == "active":
            return "phone" if c.get("onPhone") else "pc"
    if audio["screenOn"] or audio["playing"]:
        return "phone"
    if time.time() - audio["since"] < AUDIO_GRACE:
        return audio["route"]  # a glance at the phone must not yank the audio back
    return "pc"


# The phone as an audio source (media) and as a hands-free gateway (calls + mic).
A2DP_UUID = "0000110a-0000-1000-8000-00805f9b34fb"
HFP_UUID = "0000111f-0000-1000-8000-00805f9b34fb"


def set_bt_audio(on):
    """Take the route at BlueZ, not only at PipeWire.

    The PipeWire card profile is no lever at all: with it "off" Android still
    sends media to this machine's A2DP sink, where it lands nowhere and the phone
    plays silently, and still lists this machine as its active headset, so a call
    or a voice note on the phone records THIS microphone instead of its own.
    Dropping the profiles at BlueZ is what Android listens to — media goes back to
    the phone speaker and the mic back to the phone. The device stays connected,
    so AVRCP, contacts and KDE Connect are untouched.

    Only A2DP is dropped. Hands-free is NOT: Android reconnects it by itself a
    few seconds later, so disconnecting it flapped every 15s and made the laptop
    the phone's headset again each time. It stays up instead (the island keeps
    answer and dial) and set_reject_sco() keeps the call and mic audio off it.
    """
    bt = state["bluetooth"]
    if not bt.get("path") or not bt.get("connected"):
        return
    try:
        dev = dbus.Interface(system.get_object("org.bluez", bt["path"]), "org.bluez.Device1")
        noop = dict(reply_handler=lambda: None, error_handler=lambda e: None)
        (dev.ConnectProfile if on else dev.DisconnectProfile)(A2DP_UUID, **noop)
        if not state["hfp"].get("ready"):
            dev.ConnectProfile(HFP_UUID, **noop)
    except dbus.DBusException:
        pass


def call_here():
    """Is a call taken on this laptop (answered or dialed from the island)?

    Only then may the phone's call audio come here. Everything else, a ringing
    call, one answered on the handset, a WhatsApp call Bluetooth never sees, a
    phone held to the ear with its screen off, keeps its audio on the phone:
    once the phone opens an SCO link, refusing new ones does not close it.
    """
    for c in calls.values():
        if c["state"] in ("active", "held") and not c.get("onPhone"):
            return True
        if c["state"] in ("dialing", "alerting") and time.time() < audio["dialUntil"]:
            return True
    return False


def headphones_first():
    """Bluetooth headphones on this laptop become the default output and mic.

    WirePlumber keeps the last output picked by hand, and buds come back as a
    new device after every music/call mode change, so without this the sound
    falls back to the laptop speaker. Runs on each new sink; a sink picked by
    hand while the buds stay connected is kept until they reconnect."""
    phone = state["bluetooth"].get("address", "").replace(":", "_")
    proc = subprocess.Popen(["pactl", "subscribe"], stdout=subprocess.PIPE, text=True)
    for line in proc.stdout:
        if "'new' on sink #" not in line:
            continue
        idx = line.rsplit("#", 1)[1].strip()
        name = next((l.split("\t")[1] for l in run("pactl", "list", "short", "sinks").splitlines()
                     if l.split("\t")[0] == idx), "")
        if not name.startswith("bluez_output.") or (phone and phone in name):
            continue
        run("pactl", "set-default-sink", name)
        mic = "bluez_input." + name[len("bluez_output."):].split(".")[0].replace("_", ":")
        if mic in run("pactl", "list", "short", "sources"):
            run("pactl", "set-default-source", mic)


def buds_on_laptop():
    """Bluetooth buds are this laptop's output. Then a call must NOT come here:
    their mic needs a second voice (SCO) link next to the phone's, and this
    MediaTek adapter cannot carry two ("urb submission failed (90)", both links
    die). The phone sends the call straight to the buds instead (they connect to
    both), one link on the phone's radio; the island still answers and hangs up."""
    return run("pactl", "get-default-sink").startswith("bluez_output.")


def activate_sco():
    """Pull the call audio over now: the island answered, the phone did not open SCO.
    Answering here means talking here, so a muted laptop mic is switched on."""
    if buds_on_laptop():
        return
    if "MUTED" in run("wpctl", "get-volume", "@DEFAULT_AUDIO_SOURCE@"):
        run("wpctl", "set-mute", "@DEFAULT_AUDIO_SOURCE@", "0")
        event("mute", muted=False)
    path = state["hfp"].get("path")
    if not path:
        return
    try:
        props = dbus.Interface(session.get_object(TEL, path), "org.freedesktop.DBus.Properties")
        props.Set(f"{TEL}.AudioGatewayTransport1", "RejectSCO", dbus.Boolean(False))
        dbus.Interface(session.get_object(TEL, path), f"{TEL}.AudioGatewayTransport1").Activate(
            reply_handler=lambda: None, error_handler=lambda e: None)
    except dbus.DBusException:
        pass


def set_reject_sco(reject):
    """Refuse the phone's SCO (call/mic audio) link while the audio lives on the phone.

    Hands-free stays connected, so Android still lists this machine as its
    headset, but with SCO refused a call or a voice note uses the phone's own mic.
    """
    path = state["hfp"].get("path")
    if not path:
        return
    try:
        props = dbus.Interface(session.get_object(TEL, path), "org.freedesktop.DBus.Properties")
        iface = f"{TEL}.AudioGatewayTransport1"
        if bool(props.Get(iface, "RejectSCO")) != reject:
            props.Set(iface, "RejectSCO", dbus.Boolean(reject))
    except dbus.DBusException:
        pass


def media_playing():
    """Is the phone itself playing media? AVRCP answers even while the card is off."""
    try:
        om = dbus.Interface(system.get_object("org.bluez", "/"), "org.freedesktop.DBus.ObjectManager")
        for path, ifaces in om.GetManagedObjects().items():
            player = ifaces.get("org.bluez.MediaPlayer1")
            if player and str(path).startswith(state["bluetooth"].get("path", "\0")):
                return str(player.get("Status", "")).lower() == "playing"
    except dbus.DBusException:
        pass
    return False


def apply_audio():
    """Point the phone's audio at the laptop, or leave it on the phone. Blocks."""
    with _audio_lock:
        want = wanted_route()
        card = bt_card()
        if card:
            active, first = card_profiles(card)
            if active and active != "off":
                audio["profile"] = active  # remember what "laptop" looks like
            profile = audio["profile"] or first
            if want == "pc" and active == "off" and profile:
                run("pactl", "set-card-profile", card, profile)
            elif want == "phone" and active not in ("", "off"):
                run("pactl", "set-card-profile", card, "off")
        # Only when the link disagrees with the route, and at most once every 15s:
        # BlueZ answers a repeated ConnectProfile with br-connection-busy and can
        # wedge there until the device is bounced. The card is a fair proxy for
        # "A2DP is up" (hands-free alone makes none).
        if (want == "pc") != bool(card) and time.time() - audio["btAt"] > 15:
            audio["btAt"] = time.time()
            GLib.idle_add(lambda: (set_bt_audio(want == "pc"), False)[1])
        here = call_here() and not buds_on_laptop()
        GLib.idle_add(lambda: (set_reject_sco(not here), False)[1])
        audio["route"] = want
        state["audio"] = {"route": want, "mode": audio["mode"], "screenOn": audio["screenOn"],
                          "playing": audio["playing"]}


def audio_now():
    """Re-route without asking the phone anything — for call state changes."""
    threading.Thread(target=lambda: (apply_audio(), GLib.idle_add(lambda: (changed(), False)[1])), daemon=True).start()


def audio_work():
    serial = state["phone"]["serial"]
    out = run("adb", "-s", serial, "shell", "dumpsys deviceidle | grep -m1 mScreenOn", timeout=4) if serial else ""
    m = re.search(r"mScreenOn=(\w+)", out)
    # No adb answer means we cannot see the screen: assume it is on, so the
    # audio stays on the phone instead of freezing on a stale "dark" reading.
    on = m.group(1) == "true" if m else True
    if on != audio["screenOn"]:
        audio["screenOn"] = on
        audio["since"] = time.time()
    audio["playing"] = media_playing()
    apply_audio()
    GLib.idle_add(lambda: (changed(), False)[1])


def on_player_props(iface, changed_props, invalidated):
    """Phone started or stopped playing — move the audio now, do not wait for the poll."""
    if "Status" in changed_props:
        audio["playing"] = str(changed_props["Status"]).lower() == "playing"
        audio_now()


def audio_tick():
    threading.Thread(target=audio_work, daemon=True).start()
    return True


def notif_props(nid):
    """Everything KDE Connect knows about one mirrored phone notification."""
    try:
        obj = session.get_object(KDEC, KDEC_ROOT + f"{dev_path()}/notifications/{nid}")
        props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
        return to_py(props.GetAll("org.kde.kdeconnect.device.notifications.notification"))
    except dbus.DBusException:
        return {}


def icon_slug(app):
    return re.sub(r"[^a-z0-9]+", "-", app.lower()).strip("-") or "app"


def load_chats():
    global chat_log
    try:
        with open(CHATS) as f:
            chat_log = json.load(f)
    except (OSError, ValueError):
        chat_log = {}
    publish_chats()


def save_chats(keep=120):
    for key in sorted(chat_log, key=lambda k: -chat_log[k]["date"])[keep:]:
        del chat_log[key]
    try:
        write_json(CHATS, chat_log)
    except OSError:
        pass


def publish_chats():
    out = []
    for key, c in chat_log.items():
        last = c["messages"][-1] if c["messages"] else {"text": "", "date": c["date"], "out": False}
        out.append({"key": key, "app": c["app"], "title": c["title"], "icon": c.get("icon", ""),
                    "replyId": c.get("replyId", ""), "date": last["date"], "body": last["text"],
                    "outgoing": last.get("out", False), "count": len(c["messages"])})
    state["chats"] = sorted(out, key=lambda c: -c["date"])[:120]
    changed()


def log_lines(key, app, title, icon, reply_id, lines):
    """Keep a notification app's messages as a thread. A stack repeats the lines
    already seen, so only what is new past the last match is appended."""
    c = chat_log.setdefault(key, {"app": app, "title": title, "icon": icon, "replyId": reply_id,
                                  "date": time.time() * 1000, "messages": []})
    c.update(app=app, title=title, replyId=reply_id)
    if icon:
        c["icon"] = icon
    seen = [m["text"] for m in c["messages"]]
    fresh = [x for x in lines if x]
    # Find the longest tail of what is stored that the new stack starts with.
    start = 0
    for n in range(min(len(seen), len(fresh)), 0, -1):
        if seen[-n:] == fresh[:n]:
            start = n
            break
    added = fresh[start:]
    if not added:
        return False
    now = time.time() * 1000
    for text in added:
        c["messages"].append({"text": text, "date": now, "out": False})
    c["messages"] = c["messages"][-200:]
    c["date"] = now
    return True


def log_sent(key, text):
    c = chat_log.get(key)
    if not c:
        return
    c["messages"].append({"text": text, "date": time.time() * 1000, "out": True})
    c["messages"] = c["messages"][-200:]
    c["date"] = c["messages"][-1]["date"]
    save_chats()
    publish_chats()
    emit_chat()


ENTITIES = [("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"), ("&apos;", "'"), ("&nbsp;", " "), ("&amp;", "&")]


def split_lines(text):
    """One notification, one message per <br/>, with the markup taken back out."""
    out = []
    for part in re.split(r"<br\s*/?>", text or "", flags=re.I):
        part = re.sub(r"<[^>]+>", "", part)
        for entity, char in ENTITIES:  # &amp; last, or "&amp;lt;" would double-decode
            part = part.replace(entity, char)
        out.append(re.sub(r"\s+", " ", part).strip())
    return out


def emit_chat():
    key = open_chat["key"]
    if key:
        c = chat_log.get(key)
        emit({"type": "chat", "key": key, "messages": c["messages"] if c else []})
    return False


def publish_notifs():
    # KDE Connect ids are counters as strings: "10" must follow "9".
    state["phoneNotifs"] = sorted(phone_notifs.values(),
                                  key=lambda n: (0, int(n["id"])) if n["id"].isdigit() else (1, n["id"]))
    changed()


def chat_key(app, title):
    return f"{app}\x00{title}"


def keep_chat_icon(app, title, src):
    """A chat's picture lives in /tmp only while its notification does. Copy it,
    so the island still shows the right face in history and after a reboot."""
    if not app or not title or not src or not os.path.exists(src):
        return chat_icons.get(chat_key(app, title), "")
    dest = os.path.join(ICON_DIR, "chat-" + icon_slug(app) + "-" + icon_slug(title)[:60] + ".png")
    try:
        if not os.path.exists(dest) or os.path.getsize(dest) != os.path.getsize(src):
            shutil.copyfile(src, dest)
    except OSError:
        return chat_icons.get(chat_key(app, title), "")
    if chat_icons.get(chat_key(app, title)) != dest:
        chat_icons[chat_key(app, title)] = dest
        state["phoneChats"] = dict(chat_icons)
        remember(phoneChats=dict(chat_icons))
    return dest


def track_notif(nid, props):
    """WhatsApp gives every chat its own icon and title, so a phone notification
    has to carry its own, not the app's. Keep what the phone actually shows."""
    app = str(props.get("appName", ""))
    if not app:
        return
    title = str(props.get("title", ""))
    phone_notifs[nid] = {"id": nid, "app": app, "title": title,
                         "text": str(props.get("text", "")), "ticker": str(props.get("ticker", "")),
                         "replyId": str(props.get("replyId", "")),
                         "icon": keep_chat_icon(app, title, str(props.get("iconPath", "")))}
    publish_notifs()
    # Only apps you can answer are conversations. A bill reminder is not a chat.
    if phone_notifs[nid]["replyId"] and log_lines(
            chat_key(app, title), app, title, phone_notifs[nid]["icon"],
            phone_notifs[nid]["replyId"], split_lines(phone_notifs[nid]["text"])):
        save_chats()
        publish_chats()
        emit_chat()


def drop_notif(nid):
    if phone_notifs.pop(nid, None):
        publish_notifs()


def learn_app_icon(props):
    """KDE Connect posts every phone notification under its own name and logo.
    The real Android app is in appName and its One UI icon lands in a temp file.
    Keep a copy: /tmp goes away on reboot, and the island wants the icon the next
    time that app appears, not only while its notification is on screen."""
    app = str(props.get("appName", ""))
    src = str(props.get("iconPath", ""))
    if not app or not src or not os.path.exists(src):
        return
    dest = os.path.join(ICON_DIR, icon_slug(app) + ".png")
    try:
        if not os.path.exists(dest) or os.path.getsize(dest) != os.path.getsize(src):
            shutil.copyfile(src, dest)
    except OSError:
        return
    if phone_apps.get(app) != dest:
        phone_apps[app] = dest
        state["phoneApps"] = dict(phone_apps)
        remember(phoneApps=dict(phone_apps))
        changed()


def load_app_icons():
    for app, path in (memory.get("phoneApps") or {}).items():
        if os.path.exists(path):
            phone_apps[str(app)] = str(path)
    for key, path in (memory.get("phoneChats") or {}).items():
        if os.path.exists(path):
            chat_icons[str(key)] = str(path)
    state["phoneApps"] = dict(phone_apps)
    state["phoneChats"] = dict(chat_icons)


def scan_notifs():
    if not state["kdeconnect"]["deviceId"]:
        return
    try:
        ids = kdec(dev_path("/notifications"), "org.kde.kdeconnect.device.notifications").activeNotifications()
    except dbus.DBusException:
        return
    live = set()
    for nid in ids:
        nid = str(nid)
        live.add(nid)
        props = notif_props(nid)
        track_notif(nid, props)
        learn_app_icon(props)
    for gone in set(phone_notifs) - live:
        drop_notif(gone)


def on_notif_posted(nid):
    # The icon rides a separate socket the desktop sometimes fetches late (or
    # times out on when several land at once), so look again a few times.
    def read(n=nid):
        props = notif_props(n)
        track_notif(n, props)
        learn_app_icon(props)
        return False
    for delay in (0, 400, 2500, 8000):
        GLib.timeout_add(delay, read)


# ------------------------------------------------------------------ commands
def active_call(path=""):
    if path and path in calls:
        return path
    for p, c in calls.items():
        if c["state"] in ("incoming", "waiting"):
            return p
    return next(iter(calls), "")


def command(cmd: dict):
    c = cmd.get("cmd")
    kd = state["kdeconnect"]["deviceId"]
    if c == "answer":
        p = active_call(cmd.get("path", ""))
        if p:
            answered_here.add(p)
            tel_call(p, "Answer")
            GLib.timeout_add(700, lambda: (activate_sco(), False)[1])
    elif c == "hangup":
        p = active_call(cmd.get("path", ""))
        if p:
            tel_call(p, "Hangup")
    elif c == "hangupAll" and state["hfp"]["path"]:
        tel_call(state["hfp"]["path"], "HangupAll", iface="AudioGateway1")
    elif c == "dial":
        number = re.sub(r"[^\d+*#]", "", str(cmd.get("number", "")))
        if not number:
            return
        audio["dialUntil"] = time.time() + 15  # this call was placed from here
        if state["hfp"]["path"]:
            tel_call(state["hfp"]["path"], "Dial", number, iface="AudioGateway1")
        elif state["phone"]["serial"]:
            # No Bluetooth: place the call on the phone itself.
            run("adb", "-s", state["phone"]["serial"], "shell", f"am start -a android.intent.action.CALL -d tel:{number}")
        else:
            event("error", message="Connect the phone over Bluetooth to call from the desktop")
    elif c == "tones" and state["hfp"]["path"]:
        tel_call(state["hfp"]["path"], "SendTones", str(cmd.get("tones", "")), iface="AudioGateway1")
    elif c == "swap" and state["hfp"]["path"]:
        tel_call(state["hfp"]["path"], "SwapCalls", iface="AudioGateway1")
    elif c == "holdAndAnswer" and state["hfp"]["path"]:
        tel_call(state["hfp"]["path"], "HoldAndAnswer", iface="AudioGateway1")
    elif c == "mute":
        run("wpctl", "set-mute", "@DEFAULT_AUDIO_SOURCE@", "toggle")
        event("mute", muted="MUTED" in run("wpctl", "get-volume", "@DEFAULT_AUDIO_SOURCE@"))
    elif c == "sms" and kd:
        text = str(cmd.get("text", ""))
        if cmd.get("threadId"):
            kdec(f"/devices/{kd}", "org.kde.kdeconnect.device.conversations").replyToConversation(
                dbus.Int64(int(cmd["threadId"])), text, dbus.Array([], signature="v"))
        else:
            addrs = dbus.Array([dbus.Struct((str(a),), signature=None, variant_level=1) for a in cmd.get("addresses", [])], signature="v")
            kdec(f"/devices/{kd}", "org.kde.kdeconnect.device.conversations").sendWithoutConversation(
                addrs, text, dbus.Array([], signature="v"))
    elif c == "thread":
        # Open a thread: send what's cached now, then the phone fills in history.
        open_thread["id"] = int(cmd.get("threadId", 0))
        emit_thread()
        if kd and open_thread["id"]:
            kdec(f"/devices/{kd}", "org.kde.kdeconnect.device.conversations").requestConversation(
                dbus.Int64(open_thread["id"]), 0, 400)
            want_attachments(open_thread["id"])
    elif c == "reply" and kd:
        kdec(f"/devices/{kd}/notifications", "org.kde.kdeconnect.device.notifications").sendReply(
            str(cmd["replyId"]), str(cmd.get("text", "")))
        if cmd.get("key"):
            log_sent(str(cmd["key"]), str(cmd.get("text", "")))
    elif c == "chat":
        open_chat["key"] = str(cmd.get("key", ""))
        emit_chat()
    elif c == "ring" and kd:
        kdec(f"/devices/{kd}/findmyphone", "org.kde.kdeconnect.device.findmyphone").ring()
    elif c == "hotspot":
        # nmcli can take many seconds; keep calls and messages responsive.
        on = bool(cmd.get("on", not state["hotspot"]))
        threading.Thread(target=lambda: GLib.idle_add(lambda: (hotspot_done(hotspot_work(on)), False)[1]), daemon=True).start()
    elif c == "audio":
        mode = str(cmd.get("mode", "follow"))
        if mode in ("follow", "pc", "phone"):
            audio["mode"] = mode
            audio_tick()
    elif c == "plugin" and kd and str(cmd.get("name")) in SHARE_PLUGINS:
        kdec(f"/devices/{kd}", "org.kde.kdeconnect.device").setPluginEnabled(
            "kdeconnect_" + str(cmd["name"]), bool(cmd.get("on")))
        kdec_scan()
    elif c == "photos":
        open_photos()
    elif c == "share" and kd:
        kdec(f"/devices/{kd}/share", "org.kde.kdeconnect.device.share").shareUrls(
            dbus.Array([str(u) for u in cmd.get("urls", [])], signature="s"))
    elif c == "clipboard" and kd:
        kdec(f"/devices/{kd}/clipboard", "org.kde.kdeconnect.device.clipboard").sendClipboard()
    elif c == "pairKdeconnect":
        for dev in kdec().devices(True, False):
            kdec(f"/devices/{dev}", "org.kde.kdeconnect.device").requestPairing()
    elif c == "refresh":
        audio_tick()
        tel_scan()
        bt_scan()
        kdec_scan()
        if state["phone"]["serial"]:
            adb_props(state["phone"]["serial"])


def open_photos():
    kd = state["kdeconnect"]["deviceId"]
    target = ""
    if kd:
        try:
            sftp = kdec(f"/devices/{kd}/sftp", "org.kde.kdeconnect.device.sftp")
            if sftp.mountAndWait(timeout=30):
                root = str(sftp.mountPoint())
                for sub in ("DCIM/Camera", "DCIM", ""):
                    cand = os.path.join(root, "storage/emulated/0", sub) if os.path.isdir(os.path.join(root, "storage")) else os.path.join(root, sub)
                    if os.path.isdir(cand):
                        target = cand
                        break
        except dbus.DBusException:
            pass
    if not target and state["phone"]["serial"]:
        # Fallback: pull the newest camera photos over adb.
        dest = os.path.expanduser("~/Pictures/Phone")
        os.makedirs(dest, exist_ok=True)
        serial = state["phone"]["serial"]
        names = run("adb", "-s", serial, "shell", "ls -t /sdcard/DCIM/Camera | head -60").splitlines()
        for n in names:
            n = n.strip()
            if n and not os.path.exists(os.path.join(dest, n)):
                run("adb", "-s", serial, "pull", f"/sdcard/DCIM/Camera/{n}", dest, timeout=60)
        target = dest
    if target:
        state["mounted"] = target
        subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        changed()
    else:
        event("error", message="Phone not reachable for photos")


def on_stdin(fd, cond):
    line = sys.stdin.readline()
    if not line:
        loop.quit()
        return False
    try:
        command(json.loads(line))
    except (ValueError, KeyError, dbus.DBusException) as e:
        event("error", message=str(e))
    return True


# ------------------------------------------------------------------ main
SUPERSEDED = 75  # exit code: a newer phoned took over, the shell must not restart this one


def take_over():
    """Be the only phoned. The shell can create the island service twice (an async
    load races a rescan), and two daemons fight over the Bluetooth audio route.
    The newest one wins, because the shell keeps the last instance it created."""
    os.makedirs(STATE_DIR, exist_ok=True)
    signal.signal(signal.SIGTERM, lambda *a: os._exit(SUPERSEDED))
    # Locked read-kill-write: two phoneds starting together take turns, so the
    # second one always sees (and replaces) the first instead of both killing.
    with open(os.path.join(STATE_DIR, "phoned.pid"), "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        try:
            old = int(f.read().strip() or 0)
            with open(f"/proc/{old}/cmdline", "rb") as c:
                if old != os.getpid() and b"phoned.py" in c.read():
                    os.kill(old, signal.SIGTERM)
        except (OSError, ValueError):
            pass
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()))


# ------------------------------------------------------------------ socket
# Other programs (the MCP server, scripts) ask phoned instead of reaching the
# phone on their own. Read-only: one JSON request line in, one JSON line out.
SOCK = os.path.join(STATE_DIR, "phoned.sock")


def answer(req):
    q = req.get("q")
    if q == "state":
        return {**state, "calls": sorted(calls.values(), key=lambda c: c["since"]),
                "conversations": sorted(threads.values(), key=lambda t: -t["date"])[:250]}
    if q == "thread":
        tid = int(req.get("threadId", 0))
        kd = state["kdeconnect"]["deviceId"]
        if kd and len(messages.get(tid, {})) < 2:  # only the last message cached: ask the phone for history
            try:
                kdec(f"/devices/{kd}", "org.kde.kdeconnect.device.conversations").requestConversation(
                    dbus.Int64(tid), 0, 400)
            except dbus.DBusException:
                pass
        msgs = sorted(messages.get(tid, {}).values(), key=lambda m: m["date"])[-int(req.get("limit", 400)):]
        return {"threadId": tid, "messages": msgs}
    if q == "chat":
        return chat_log.get(str(req.get("key", "")), {"messages": []})
    return {"error": f"unknown query {q!r}; try state, thread or chat"}


def on_client(conn, buf):
    try:
        chunk = conn.recv(65536)
    except OSError:
        chunk = b""
    buf += chunk
    if chunk and b"\n" not in buf and len(buf) < 65536:
        return True  # keep reading
    try:
        reply = answer(json.loads(buf.split(b"\n", 1)[0] or b"{}"))
    except (ValueError, TypeError, AttributeError) as e:
        reply = {"error": f"bad request: {e}"}
    try:
        conn.sendall(json.dumps(reply, ensure_ascii=False).encode() + b"\n")
    except OSError:
        pass
    conn.close()
    return False


def on_accept(srv, _cond):
    try:
        conn, _ = srv.accept()
    except OSError:
        return True
    conn.settimeout(5)  # a stuck client must not freeze phoned
    buf = bytearray()
    GLib.io_add_watch(conn.fileno(), GLib.IO_IN | GLib.IO_HUP, lambda *_: on_client(conn, buf))
    return True


def serve_socket():
    try:
        os.unlink(SOCK)
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)  # 0600 from the start: messages are private
    try:
        srv.bind(SOCK)
    finally:
        os.umask(old)
    srv.listen(8)
    GLib.io_add_watch(srv.fileno(), GLib.IO_IN, lambda *_: on_accept(srv, None))
    return srv


def main():
    global loop
    take_over()
    serve_socket()
    session.add_signal_receiver(on_tel_added, "InterfacesAdded", "org.freedesktop.DBus.ObjectManager", TEL)
    session.add_signal_receiver(on_tel_removed, "InterfacesRemoved", "org.freedesktop.DBus.ObjectManager", TEL)
    session.add_signal_receiver(on_tel_props, "PropertiesChanged", "org.freedesktop.DBus.Properties", TEL, path_keyword="path")
    for member in ("conversationCreated", "conversationUpdated", "conversationRemoved", "refreshed", "callReceived",
                   "notificationPosted", "notificationUpdated", "notificationRemoved", "allNotificationsRemoved",
                   "attachmentReceived", "deviceListChanged", "reachableChanged", "pairStateChanged",
                   "localCacheSynchronized"):
        session.add_signal_receiver(on_kdec_signal, member, None, KDEC, member_keyword="member", path_keyword="path")
    session.add_signal_receiver(lambda name, old, new: GLib.timeout_add(500, lambda: (tel_scan() if name == TEL else kdec_scan(), False)[1])
                                if name in (TEL, KDEC) else None, "NameOwnerChanged", "org.freedesktop.DBus")
    system.add_signal_receiver(lambda *a, **k: GLib.timeout_add(200, lambda: (bt_scan(), False)[1]), "PropertiesChanged",
                               "org.freedesktop.DBus.Properties", "org.bluez", arg0="org.bluez.Device1")
    system.add_signal_receiver(on_player_props, "PropertiesChanged", "org.freedesktop.DBus.Properties",
                               "org.bluez", arg0="org.bluez.MediaPlayer1")

    load_contacts()
    load_app_icons()
    load_chats()
    tel_scan()
    bt_scan()
    kdec_scan()
    start_track()
    threading.Thread(target=headphones_first, daemon=True).start()
    audio_tick()
    GLib.timeout_add_seconds(3, audio_tick)
    GLib.timeout_add_seconds(20, bt_scan)
    GLib.timeout_add_seconds(15, wifi_reconnect)
    GLib.timeout_add_seconds(60, kdec_scan)
    GLib.io_add_watch(sys.stdin.fileno(), GLib.IO_IN | GLib.IO_HUP, on_stdin)
    changed()
    loop = GLib.MainLoop()
    loop.run()


if __name__ == "__main__":
    main()
