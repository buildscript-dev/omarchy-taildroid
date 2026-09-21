.pragma library

function setting(settings, name, fallback) {
  if (!settings) return fallback
  var value = settings[name]
  return value === undefined || value === null ? fallback : value
}

function intSetting(settings, name, fallback, min, max) {
  var n = parseInt(String(setting(settings, name, fallback)), 10)
  if (!isFinite(n)) n = fallback
  if (n < min) n = min
  if (n > max) n = max
  return n
}

function boolSetting(settings, name, fallback) {
  var value = setting(settings, name, fallback)
  if (typeof value === "boolean") return value
  var text = String(value).toLowerCase()
  if (text === "true" || text === "1" || text === "on") return true
  if (text === "false" || text === "0" || text === "off") return false
  return fallback
}

function parseJson(raw) {
  try {
    return JSON.parse(String(raw || ""))
  } catch (e) {
    return null
  }
}

function elide(text, max) {
  var value = String(text || "").replace(/\s+/g, " ").trim()
  if (value.length <= max) return value
  return value.substring(0, Math.max(0, max - 1)) + "…"
}

function maskIPv4(ip) {
  var parts = String(ip || "").split(".")
  if (parts.length !== 4) return String(ip || "")
  return parts[0] + ".*.*.*"
}

function maskHostPort(value) {
  var s = String(value || "")
  var m = s.match(/^(\d{1,3}(?:\.\d{1,3}){3})(?::(\d{1,5}))?$/)
  if (m) return maskIPv4(m[1]) + (m[2] ? ":" + m[2] : "")
  return maskText(s)
}

function maskText(text) {
  var s = String(text || "")
  s = s.replace(/\b[A-Za-z0-9._-]+\.tail[0-9a-fA-F]+\.ts\.net\b/g, function(fqdn) {
    return String(fqdn.split(".")[0] || "host") + ".*****.ts.net"
  })
  s = s.replace(/\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{0,4}\b/g, "*:*:*:*")
  s = s.replace(/\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b/g, function(_, a) {
    return a + ".*.*.*"
  })
  return s
}

function displayDeviceName(device) {
  if (!device) return "Android"
  if (device.name && device.name !== device.serial) return String(device.name)
  return maskHostPort(device.serial || "Android")
}

function deviceSubtitle(device) {
  if (!device) return ""
  var bits = []
  if (device.state && device.state !== "device") bits.push(device.state)
  else bits.push(device.transport === "tcp" ? "Tailscale / TCP" : "USB")
  if (device.battery !== undefined && device.battery !== null) bits.push(device.battery + "%")
  if (device.serial) bits.push(maskHostPort(device.serial))
  return bits.join(" · ")
}

function peerSubtitle(peer) {
  if (!peer) return ""
  var bits = []
  bits.push(peer.online ? "Online" : "Offline")
  if (peer.ip) bits.push(maskHostPort(peer.ip))
  return bits.join(" · ")
}

function pickDevice(devices, preferredSerial) {
  var list = devices || []
  var preferred = String(preferredSerial || "")
  if (preferred) {
    for (var i = 0; i < list.length; i++) {
      if (list[i].serial === preferred && list[i].state === "device") return list[i]
    }
  }
  for (var j = 0; j < list.length; j++) {
    if (list[j].state === "device") return list[j]
  }
  return list.length > 0 ? list[0] : null
}

function scrcpyCommand(scrcpyPath, device, settings) {
  var cmd = [scrcpyPath || "scrcpy"]
  if (device && device.serial) {
    cmd.push("--serial")
    cmd.push(String(device.serial))
  }
  cmd.push("--keyboard=uhid")
  cmd.push("--mouse=uhid")
  var title = displayDeviceName(device)
  cmd.push("--window-title=" + title)
  if (boolSetting(settings, "stayAwake", true)) cmd.push("--stay-awake")
  if (boolSetting(settings, "turnScreenOff", false)) cmd.push("--turn-screen-off")
  if (boolSetting(settings, "alwaysOnTop", false)) cmd.push("--always-on-top")
  if (!boolSetting(settings, "audioEnabled", true)) cmd.push("--no-audio")
  if (!boolSetting(settings, "clipboardAutosync", true)) cmd.push("--no-clipboard-autosync")
  var maxSize = intSetting(settings, "maxSize", 0, 0, 4096)
  if (maxSize > 0) {
    cmd.push("--max-size")
    cmd.push(String(maxSize))
  }
  return cmd
}
