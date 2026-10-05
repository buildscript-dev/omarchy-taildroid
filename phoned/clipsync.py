#!/usr/bin/env python3
"""One clipboard for the phone and the laptop, over adb.

Android 10+ refuses clipboard reads from background apps, so KDE Connect
on the phone never sees a copy. The adb shell user may read it, which is
how scrcpy syncs. This runs scrcpy's server with control only (no video,
no audio) and:

  phone text copy    -> laptop clipboard
  laptop text copy   -> phone clipboard
  laptop image copy  -> phone gallery, album "Clipboard" (Android has no
                        way for adb to put an image ON the clipboard)
  phone image, Share -> KDE Connect -> laptop clipboard (a phone image copy
                        is not readable over adb, so Share is the way in)
"""
import hashlib
import os
import re
import socket
import struct
import subprocess
import threading
import time
import urllib.parse

JAR = "/usr/share/scrcpy/scrcpy-server"
REMOTE = "/data/local/tmp/scrcpy-server.jar"
SCID = "0c11b0a2"
PORT = 27199
TEXT_MAX = 262000  # scrcpy drops a control message above 256 KiB
ALBUM = "/sdcard/Pictures/Clipboard"
IMAGES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
          "webp": "image/webp", "gif": "image/gif"}

link = {"sock": None, "dev": None}
seen = [b""]  # last content that crossed, either way: stops the echo


def out(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout


def raw(*cmd):
    return subprocess.run(cmd, capture_output=True, timeout=20).stdout


def serial():
    found = re.findall(r"^(\S+)\tdevice$", out("adb", "devices"), re.M)
    return found[0] if found else None


def read(stream, n):
    data = stream.read(n)
    if len(data) < n:
        raise ConnectionError("phone closed the clipboard link")
    return data


def messages(stream):
    """Yield clipboard texts from a scrcpy device-message stream."""
    while True:
        kind = read(stream, 1)[0]
        if kind != 0:  # we never ask for an ack, so only clipboard can come
            raise ConnectionError(f"unexpected device message {kind}")
        size = struct.unpack(">I", read(stream, 4))[0]
        yield read(stream, size)


def set_clipboard(text):
    """scrcpy SET_CLIPBOARD: type 9, sequence 0 (no ack), paste off, text."""
    text = text[:TEXT_MAX]
    return struct.pack(">BQBI", 9, 0, 0, len(text)) + text


def to_laptop(text):
    # KDE Connect also sends the laptop clipboard to the phone, which then
    # reports it back here. Writing the same text again would loop forever.
    if text and text != seen[0] and text != raw("wl-paste", "-n", "-t", "text"):
        seen[0] = text
        subprocess.run(["wl-copy"], input=text)


def laptop_copied():
    types = out("wl-paste", "-l").split()
    image = next((t for t in types if t in IMAGES.values()), None)
    if any(t.startswith("text/plain") for t in types):
        text = raw("wl-paste", "-n", "-t", "text")
        if text and text != seen[0] and link["sock"]:
            seen[0] = text
            link["sock"].sendall(set_clipboard(text))
    elif image and link["dev"]:
        data = raw("wl-paste", "-t", image)
        if data and data != seen[0]:
            seen[0] = data
            # Named by content, so the same image never lands twice.
            # ponytail: the album only grows; prune old files if it gets big.
            name = hashlib.sha1(data).hexdigest()[:12] + "." + image.split("/")[1]
            path = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), name)
            with open(path, "wb") as f:
                f.write(data)
            out("adb", "-s", link["dev"], "shell", "mkdir", "-p", ALBUM)
            out("adb", "-s", link["dev"], "push", path, f"{ALBUM}/{name}")
            os.unlink(path)


def shared_file(line):
    """Path of an image in a KDE Connect shareReceived signal line, or None."""
    m = re.search(r'string "file://(.+\.(\w+))"', line)
    if m and m.group(2).lower() in IMAGES:
        return urllib.parse.unquote(m.group(1)), IMAGES[m.group(2).lower()]


def watch(cmd, handle):
    """Run cmd for ever and call handle(line) for each line it prints."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
    for line in proc.stdout:
        try:
            handle(line)
        except Exception as e:
            print(e, flush=True)
    os._exit(1)  # the watcher died; systemd starts us again


def phone_shared(line):
    hit = shared_file(line)
    if hit:
        with open(hit[0], "rb") as f:
            seen[0] = f.read()  # so it is not pushed straight back to the phone
        subprocess.run(["wl-copy", "-t", hit[1]], input=seen[0])


def sync(dev):
    # The server refuses a client of another version, so ask scrcpy for its own.
    version = re.search(r"scrcpy (\S+)", out("scrcpy", "--version")).group(1)
    out("adb", "-s", dev, "push", JAR, REMOTE)
    out("adb", "-s", dev, "forward", f"tcp:{PORT}", f"localabstract:scrcpy_{SCID}")
    server = subprocess.Popen(
        ["adb", "-s", dev, "shell", f"CLASSPATH={REMOTE}", "app_process", "/",
         "com.genymobile.scrcpy.Server", version, f"scid={SCID}", "tunnel_forward=true",
         "video=false", "audio=false", "control=true", "send_device_meta=false",
         "cleanup=false", "power_on=false"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(20):
            # adb accepts the forward before the server listens; the server's
            # dummy byte is the proof that the link is real.
            with socket.create_connection(("127.0.0.1", PORT)) as sock:
                stream = sock.makefile("rb")
                if stream.read(1):
                    link.update(sock=sock, dev=dev)
                    for text in messages(stream):
                        to_laptop(text)
            if server.poll() is not None:
                break
            time.sleep(0.5)
    finally:
        link.update(sock=None, dev=None)
        server.kill()


def demo():
    import io
    stream = io.BytesIO(b"\x00\x00\x00\x00\x02hi\x00\x00\x00\x00\x03a\nb")
    got = []
    try:
        for text in messages(stream):
            got.append(text)
    except ConnectionError:
        pass
    assert got == [b"hi", b"a\nb"], got
    assert set_clipboard(b"hi") == b"\x09" + bytes(8) + b"\x00\x00\x00\x00\x02hi"
    assert len(set_clipboard(b"x" * 300000)) == 14 + TEXT_MAX
    assert shared_file('   string "file:///home/u/My%20Pic.JPG"') == ("/home/u/My Pic.JPG", "image/jpeg")
    assert shared_file('   string "file:///home/u/notes.pdf"') is None
    print("ok")


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["demo"]:
        demo()
        sys.exit()
    threading.Thread(target=watch, daemon=True, args=(
        ["wl-paste", "--watch", "echo"], lambda _: laptop_copied())).start()
    threading.Thread(target=watch, daemon=True, args=(
        ["dbus-monitor", "--session", "type='signal',"
         "interface='org.kde.kdeconnect.device.share',member='shareReceived'"],
        phone_shared)).start()
    while True:
        try:
            dev = serial()
            if dev:
                sync(dev)
        except Exception as e:  # adb drops with Wi-Fi; keep trying
            print(e, flush=True)
        time.sleep(5)
