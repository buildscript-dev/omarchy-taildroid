#!/usr/bin/env python3
"""ADB / Tailscale / Bluetooth status helper for Taildroid."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys

HOSTPORT_RE = re.compile(r"^[A-Za-z0-9.:_-]{3,80}$")
CODE_RE = re.compile(r"^\d{6}$")
SERIAL_RE = re.compile(r"^[A-Za-z0-9.:_-]{1,80}$")


def which(name: str) -> str:
    return shutil.which(name) or ""


def run(cmd: list[str], timeout: float = 6.0, stdin_text: str | None = None) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=stdin_text,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError:
        return 127, "", f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"


def fail(message: str, **extra) -> None:
    print(json.dumps({"ok": False, "error": mask_text(message), **extra}, ensure_ascii=True))
    sys.exit(1)


def ok(payload: dict) -> None:
    payload = dict(payload)
    payload["ok"] = True
    print(json.dumps(payload, ensure_ascii=True))


def split_lines(text: str) -> list[str]:
    return str(text or "").splitlines()


def last_line(text: str) -> str:
    lines = [line.strip() for line in split_lines(text) if line.strip()]
    return lines[-1] if lines else ""


def mask_text(text: str) -> str:
    """Redact addresses in user-visible helper text. Connection args stay raw."""
    s = str(text or "")
    s = re.sub(
        r"\b[A-Za-z0-9._-]+\.tail[0-9a-fA-F]+\.ts\.net\b",
        lambda m: f"{m.group(0).split('.')[0]}.*****.ts.net",
        s,
    )
    s = re.sub(r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{0,4}\b", "*:*:*:*", s)
    s = re.sub(
        r"\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b",
        lambda m: f"{m.group(1)}.*.*.*",
        s,
    )
    return s


def require_hostport(value: str) -> str:
    value = (value or "").strip()
    if not HOSTPORT_RE.match(value) or ":" not in value:
        fail("Address must look like host:port")
    host, port = value.rsplit(":", 1)
    if not port.isdigit() or not (1 <= int(port) <= 65535):
        fail("Port must be 1-65535")
    if not host:
        fail("Address must look like host:port")
    return value


def require_code(value: str) -> str:
    value = (value or "").strip()
    if not CODE_RE.match(value):
        fail("Pairing code must be six digits")
    return value


def require_serial(value: str) -> str:
    value = (value or "").strip()
    if not SERIAL_RE.match(value):
        fail("Invalid device serial")
    return value


def is_preferred(host: str, dns: str, preferred: str) -> bool:
    needle = (preferred or "").strip().lower()
    if not needle:
        return False
    host_l = (host or "").lower()
    slug = host_l.replace(" ", "-")
    dns_l = (dns or "").lower()
    return host_l == needle or slug == needle or dns_l == needle or dns_l.startswith(needle + ".")


def parse_adb_devices(text: str) -> list[dict]:
    devices = []
    started = False
    for raw in split_lines(text):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("List of devices"):
            started = True
            continue
        if not started or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        extras = {}
        for token in parts[2:]:
            if ":" in token:
                key, val = token.split(":", 1)
                extras[key] = val.replace("_", " ")
        transport = "tcp" if ":" in serial else "usb"
        model = extras.get("model", "")
        devices.append(
            {
                "serial": serial,
                "state": state,
                "model": model,
                "product": extras.get("product", ""),
                "device": extras.get("device", ""),
                "usb": extras.get("usb", ""),
                "transport": transport,
                "authorized": state == "device",
                "battery": None,
                "name": model or serial,
            }
        )
    return devices


def enrich_device(dev: dict) -> dict:
    if dev.get("state") != "device":
        return dev
    serial = dev["serial"]
    code, out, _err = run(
        ["adb", "-s", serial, "shell", "getprop", "ro.product.model"],
        timeout=4,
    )
    model = out.strip()
    if code == 0 and model:
        dev["model"] = model
        dev["name"] = model
    code, out, _err = run(
        ["adb", "-s", serial, "shell", "dumpsys", "battery"],
        timeout=4,
    )
    if code == 0:
        for line in split_lines(out):
            line = line.strip()
            if line.startswith("level:"):
                try:
                    dev["battery"] = int(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
                break
    return dev


def list_tailscale(preferred: str) -> list[dict]:
    if not which("tailscale"):
        return []
    code, out, _err = run(["tailscale", "status", "--json"], timeout=5)
    if code != 0:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    peers = []
    for peer in (data.get("Peer") or {}).values():
        os_name = str(peer.get("OS") or "").lower()
        if os_name != "android":
            continue
        ips = [ip for ip in (peer.get("TailscaleIPs") or []) if ":" not in ip]
        dns = str(peer.get("DNSName") or "").rstrip(".")
        host = str(peer.get("HostName") or dns.split(".")[0] or "")
        peers.append(
            {
                "name": host or dns or (ips[0] if ips else "android"),
                "dns": dns,
                "ip": ips[0] if ips else "",
                "online": bool(peer.get("Online")),
                "os": "android",
                "preferred": is_preferred(host, dns, preferred),
            }
        )
    peers.sort(key=lambda p: (not p["preferred"], not p["online"], p["name"]))
    return peers


def list_bluetooth() -> list[dict]:
    if not which("bluetoothctl"):
        return []
    _code, out, _err = run(["bluetoothctl", "devices", "Connected"], timeout=3)
    devices = []
    for line in split_lines(out):
        parts = line.split(None, 2)
        if len(parts) < 2 or parts[0] != "Device":
            continue
        address = parts[1]
        name = parts[2] if len(parts) > 2 else address
        devices.append({"address": address, "name": name, "connected": True})
    return devices


def cmd_status(argv: list[str]) -> None:
    preferred = argv[0] if argv else ""
    adb_path = which("adb")
    scrcpy_path = which("scrcpy")
    devices = []
    adb_error = ""
    if adb_path:
        code, out, err = run(["adb", "devices", "-l"], timeout=6)
        if code == 0:
            devices = [enrich_device(dev) for dev in parse_adb_devices(out)]
        else:
            adb_error = last_line(err or out or "adb devices failed")
    ok(
        {
            "adb": bool(adb_path),
            "scrcpy": bool(scrcpy_path),
            "adbPath": adb_path,
            "scrcpyPath": scrcpy_path,
            "devices": devices,
            "tailscale": list_tailscale(preferred),
            "bluetooth": list_bluetooth(),
            "adbError": adb_error,
        }
    )


def cmd_connect(argv: list[str]) -> None:
    if not which("adb"):
        fail("adb is not installed")
    if not argv:
        fail("connect needs host:port")
    addr = require_hostport(argv[0])
    code, out, err = run(["adb", "connect", addr], timeout=10)
    text = last_line((out or "") + "\n" + (err or ""))
    lowered = text.lower()
    if code != 0 or "failed" in lowered or "cannot" in lowered or "refused" in lowered:
        fail(text or f"adb connect {addr} failed")
    ok({"message": mask_text(text or f"Connected {addr}"), "address": addr})


def cmd_pair(argv: list[str]) -> None:
    if not which("adb"):
        fail("adb is not installed")
    if not argv:
        fail("pair needs host:port")
    if len(argv) > 1:
        fail("pair takes host:port; send the six-digit code on stdin")
    addr = require_hostport(argv[0])
    # Pairing codes must not appear in argv; they are visible in /proc/<pid>/cmdline.
    code_value = require_code(sys.stdin.readline())
    code, out, err = run(["adb", "pair", addr], timeout=15, stdin_text=code_value + "\n")
    text = last_line((out or "") + "\n" + (err or ""))
    if text.startswith("Enter pairing code:"):
        text = text[len("Enter pairing code:") :].lstrip()
    if code != 0 or "failed" in text.lower():
        fail(text or f"adb pair {addr} failed")
    ok({"message": mask_text(text or f"Paired {addr}"), "address": addr})


def cmd_disconnect(argv: list[str]) -> None:
    if not which("adb"):
        fail("adb is not installed")
    if not argv:
        fail("disconnect needs a serial or host:port")
    serial = require_serial(argv[0])
    code, out, err = run(["adb", "disconnect", serial], timeout=8)
    text = last_line((out or "") + "\n" + (err or ""))
    if code != 0:
        fail(text or "adb disconnect failed")
    ok({"message": mask_text(text or f"Disconnected {serial}")})


def cmd_connect_tailscale(argv: list[str]) -> None:
    preferred = argv[0] if argv else ""
    port = argv[1] if len(argv) > 1 else "5555"
    if not port.isdigit() or not (1 <= int(port) <= 65535):
        fail("Port must be 1-65535")
    peers = list_tailscale(preferred)
    online = [p for p in peers if p.get("online") and p.get("ip")]
    if not online:
        fail("No online Android device on Tailscale")
    chosen = online[0]
    cmd_connect([f"{chosen['ip']}:{port}"])


def main() -> None:
    if len(sys.argv) < 2:
        fail("usage: phone.py status|connect|pair|disconnect|connect-tailscale")
    cmd = sys.argv[1]
    argv = sys.argv[2:]
    if cmd == "status":
        cmd_status(argv)
    elif cmd == "connect":
        cmd_connect(argv)
    elif cmd == "pair":
        cmd_pair(argv)
    elif cmd == "disconnect":
        cmd_disconnect(argv)
    elif cmd == "connect-tailscale":
        cmd_connect_tailscale(argv)
    else:
        fail(f"unknown command {cmd}")


if __name__ == "__main__":
    main()
