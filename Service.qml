import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import "Model.js" as Model

Item {
  id: root

  property var settings: ({})
  // Only one Service may run phoned: two daemons fight over the Bluetooth audio
  // route and bounce A2DP every few seconds. The bar widget sets this false.
  property bool runPhoned: true
  property bool poll: true  // the panel's copy polls only while open; the island's always
  property bool _startAfterRefresh: false

  property bool adbInstalled: false
  property bool scrcpyInstalled: false
  property string adbPath: ""
  property string scrcpyPath: ""
  property var devices: []
  property var tailscalePeers: []
  property var bluetoothDevices: []
  property bool refreshing: false
  property string statusText: "Checking…"
  property string actionStatus: ""
  property string lastError: ""
  property string controllingSerial: ""
  property int _desired: -1
  readonly property bool sessionRunning: scrcpyProcess.running
  readonly property bool controlling: _desired === -1 ? sessionRunning : (_desired === 1)
  // Status polls must not swallow the header switch. Only in-flight control
  // actions count as busy, matching how ToggleSwitch is documented to behave.
  readonly property bool busy: actionProcess.running || (_desired === 0 && sessionRunning)
  readonly property int refreshIntervalSec: Model.intSetting(settings, "refreshIntervalSec", 15, 5, 120)
  readonly property int adbPort: Model.intSetting(settings, "adbPort", 5555, 1, 65535)
  readonly property string preferredPeer: String(Model.setting(settings, "preferredPeer", ""))
  readonly property string preferredSerial: String(Model.setting(settings, "preferredSerial", ""))
  readonly property var selectedDevice: Model.pickDevice(devices, controllingSerial || preferredSerial)
  readonly property bool hasReadyDevice: !!(selectedDevice && selectedDevice.state === "device")
  readonly property var onlinePeer: {
    for (var i = 0; i < tailscalePeers.length; i++) {
      if (tailscalePeers[i].online && tailscalePeers[i].ip) return tailscalePeers[i]
    }
    return null
  }

  property string _statusOutput: ""
  property string _statusError: ""
  property string _actionOutput: ""
  property string _actionError: ""
  property string _pendingAction: ""
  property string _pendingStdin: ""
  property bool _startAfterConnect: false
  property string _scrcpyError: ""

  function helperPath() {
    var raw = Qt.resolvedUrl("phone.py").toString()
    if (raw.indexOf("file://") === 0) raw = raw.substring(7)
    return decodeURIComponent(raw)
  }

  function elideStatus(text) {
    return Model.elide(Model.maskText(text), 180)
  }

  function setAction(text) {
    actionStatus = Model.maskText(text)
    if (text) actionStatusTimer.restart()
  }

  function missingToolsText() {
    var missing = []
    if (!adbInstalled) missing.push("android-tools")
    if (!scrcpyInstalled) missing.push("scrcpy")
    return "Install " + missing.join(" and ") + " with omarchy pkg add"
  }

  function statusFrom(parsed) {
    var ready = Model.pickDevice(parsed.devices || [], controllingSerial || preferredSerial)
    var peer = null
    var peers = parsed.tailscale || []
    for (var i = 0; i < peers.length; i++) {
      if (peers[i].online && peers[i].ip) { peer = peers[i]; break }
    }
    if (!(parsed.adb === true) || !(parsed.scrcpy === true)) {
      var missing = []
      if (parsed.adb !== true) missing.push("android-tools")
      if (parsed.scrcpy !== true) missing.push("scrcpy")
      return "Install " + missing.join(" and ") + " with omarchy pkg add"
    }
    if (sessionRunning) return "Mouse and keyboard are on the phone"
    if (ready && ready.state === "device") return "Ready — Super+Shift+I to take control"
    if (peer) return peer.name + " is on Tailscale — connect wireless debugging"
    return "No phone on ADB yet"
  }

  function refresh() {
    if (statusProcess.running) return
    _statusOutput = ""
    _statusError = ""
    refreshing = true
    statusProcess.command = ["python3", helperPath(), "status", preferredPeer]
    statusProcess.running = true
  }

  function applyStatus(raw) {
    var parsed = Model.parseJson(raw)
    if (!parsed || parsed.ok !== true) {
      lastError = elideStatus((parsed && parsed.error) ? parsed.error : "Failed to read phone status")
      statusText = lastError
      return
    }
    adbInstalled = parsed.adb === true
    scrcpyInstalled = parsed.scrcpy === true
    adbPath = String(parsed.adbPath || "")
    scrcpyPath = String(parsed.scrcpyPath || "")
    devices = parsed.devices || []
    tailscalePeers = parsed.tailscale || []
    bluetoothDevices = parsed.bluetooth || []
    if (parsed.adbError) lastError = elideStatus(parsed.adbError)
    statusText = statusFrom(parsed)
  }

  function runAction(args, pending, stdinText) {
    if (actionProcess.running) return
    _actionOutput = ""
    _actionError = ""
    _pendingAction = pending || ""
    _pendingStdin = stdinText ? String(stdinText) : ""
    actionProcess.stdinEnabled = root._pendingStdin !== ""
    actionProcess.command = ["python3", helperPath()].concat(args)
    actionProcess.running = true
  }

  function connectAddress(addr) {
    if (!adbInstalled) {
      lastError = missingToolsText()
      return
    }
    setAction("Connecting " + Model.maskHostPort(addr) + "…")
    runAction(["connect", addr], "connect")
  }

  function connectTailscale() {
    if (!adbInstalled) {
      lastError = missingToolsText()
      return
    }
    setAction("Connecting over Tailscale…")
    runAction(["connect-tailscale", preferredPeer, String(adbPort)], "connect")
  }

  function pair(addr, code) {
    var host = String(addr || "").trim()
    var pin = String(code || "").trim()
    if (!host || !pin) {
      lastError = "Enter the wireless pairing address and six-digit code, then Pair"
      return
    }
    setAction("Pairing " + host + "…")
    // Do not pass the PIN on argv; same-user processes can read the helper command line.
    runAction(["pair", host], "pair", pin)
  }

  function disconnectDevice(serial) {
    if (!serial) return
    setAction("Disconnecting " + Model.maskHostPort(serial) + "…")
    runAction(["disconnect", serial], "disconnect")
  }

  function startControl(device) {
    var target = device || selectedDevice
    if (sessionRunning) return
    if (!scrcpyInstalled || !adbInstalled) {
      lastError = missingToolsText()
      return
    }
    if (!target || target.state !== "device") {
      if (!poll && !_startAfterRefresh) {  // device list may be stale: look again first
        _startAfterRefresh = true
        refresh()
        return
      }
      if (onlinePeer && !_startAfterConnect) {
        _startAfterConnect = true
        connectTailscale()
        return
      }
      lastError = "No authorized Android device. Pair wireless debugging or plug in USB."
      _startAfterConnect = false
      return
    }
    _startAfterConnect = false
    _desired = 1
    controllingSerial = target.serial
    lastError = ""
    _scrcpyError = ""
    scrcpyProcess.command = mirrorCommand(target, [])
    scrcpyProcess.running = true
    statusText = "Mouse and keyboard are on the phone"
    setAction("Taking control of " + Model.displayDeviceName(target))
  }

  function stopControl() {
    if (!sessionRunning && !scrcpyProcess.running) {
      _desired = -1
      return
    }
    _desired = 0
    scrcpyProcess.running = false
    setAction("Releasing the phone")
  }

  function toggleControl() {
    if (controlling) stopControl()
    else startControl(selectedDevice)
  }

  function handleActionExit(exitCode) {
    var stdout = String(actionStdout.text || _actionOutput || "")
    var stderr = String(actionStderr.text || _actionError || "")
    var parsed = Model.parseJson(stdout)
    var pending = _pendingAction
    var shouldStart = _startAfterConnect && pending === "connect"
    _pendingAction = ""
    if (exitCode !== 0 || !parsed || parsed.ok !== true) {
      _startAfterConnect = false
      lastError = elideStatus((parsed && parsed.error) || stderr || stdout || "Phone command failed")
      actionStatus = lastError
      return
    }
    lastError = ""
    setAction(parsed.message || "OK")
    delayedRefresh.restart()
    if (shouldStart) delayedStart.restart()
  }

  // ---------------------------------------------------------------- continuity
  // Skinned mirror + phoned (calls, messages, battery, Bluetooth, KDE Connect).
  readonly property string pluginDir: {
    var raw = Qt.resolvedUrl(".").toString()
    if (raw.indexOf("file://") === 0) raw = raw.substring(7)
    return decodeURIComponent(raw).replace(/\/$/, "")
  }
  readonly property string mirrorBin: Quickshell.env("HOME") + "/.local/bin/taildroid-mirror"

  // ~/.config/taildroid/mirror.json overrides these (skin, color, codec…).
  property var mirrorConfig: ({})
  FileView {
    path: Quickshell.env("HOME") + "/.config/taildroid/mirror.json"
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: { try { root.mirrorConfig = JSON.parse(text() || "{}") } catch (e) { root.mirrorConfig = {} } }
  }
  function mirrorOpt(name, fallback) {
    var v = mirrorConfig[name]
    return v === undefined || v === null || v === "" ? fallback : v
  }
  function mirrorCommand(device, extra) {
    var cmd = [mirrorBin, "--serial", String(device.serial),
      "--codec", String(mirrorOpt("codec", "h265")),
      "--bitrate", String(mirrorOpt("bitrateMbps", 24)),
      "--fps", String(mirrorOpt("maxFps", 120)),
      "--frame", String(mirrorOpt("frame", "s24")),
      "--color", String(mirrorOpt("frameColor", ""))]
    var skin = String(mirrorOpt("skin", ""))
    if (skin !== "") cmd.push("--skin", skin)
    if (mirrorOpt("screenOff", true) === true) cmd.push("--screen-off")
    if (mirrorOpt("audio", true) !== true) cmd.push("--no-audio")
    if (mirrorOpt("clipboard", true) !== true) cmd.push("--no-clipboard")
    var maxSize = Number(mirrorOpt("maxSize", 0))
    if (maxSize > 0) cmd.push("--max-size", String(maxSize))
    return cmd.concat(extra || [])
  }
  function readySerial() {
    if (pstate.phone && pstate.phone.serial) return pstate.phone.serial
    return hasReadyDevice ? selectedDevice.serial : ""
  }
  // Extra windows: Samsung DeX / desktop mode, one app in its own window, webcam.
  function openDex() {
    var serial = readySerial()
    if (!serial) { lastError = "Connect the phone first"; return }
    Quickshell.execDetached(mirrorCommand({ serial: serial }, ["--frame", "none", "--new-display", String(mirrorOpt("dexDisplay", "1920x1080/240")), "--flex", "--title", "Galaxy DeX"]))
  }
  function openApp(pkg) {
    var serial = readySerial()
    if (!serial || !pkg) return
    Quickshell.execDetached(mirrorCommand({ serial: serial }, ["--frame", "none", "--new-display", "1080x2340/420", "--flex", "--start-app", String(pkg), "--no-audio"]))
  }
  function webcam() {
    var serial = readySerial()
    if (!serial) { lastError = "Connect the phone first"; return }
    Quickshell.execDetached(["sh", "-c", "dev=$(ls /sys/devices/virtual/video4linux 2>/dev/null | head -1); " +
      "if [ -z \"$dev\" ]; then notify-send 'Phone webcam' 'Install v4l2loopback-dkms and load it: sudo modprobe v4l2loopback exclusive_caps=1 card_label=\"Galaxy Camera\"'; exit 1; fi; " +
      "exec scrcpy --serial \"$1\" --video-source=camera --camera-facing=back --camera-size=1920x1080 --no-audio --no-window --no-control --v4l2-sink=/dev/$dev", "sh", serial])
  }

  property var pstate: ({ phone: {}, battery: { level: -1 }, signal: {}, bluetooth: {}, hfp: {}, calls: [], kdeconnect: {}, conversations: [], audio: { route: "", mode: "follow", screenOn: false }, phoneApps: {}, phoneChats: {}, phoneNotifs: [], chats: [] })
  readonly property var calls: pstate.calls || []
  readonly property var conversations: pstate.conversations || []
  // "pc" while the phone is idle, "phone" while it is in your hand.
  readonly property var audio: pstate.audio || ({ route: "", mode: "follow", screenOn: false })
  property var threadMessages: []
  property int openThreadId: 0
  property bool micMuted: false
  signal phoneEvent(var ev)

  function phonedSend(obj) {
    if (!phoned.running) return
    phoned.write(JSON.stringify(obj) + "\n")
  }
  function answer(path) { phonedSend({ cmd: "answer", path: path || "" }) }
  function hangup(path) { phonedSend({ cmd: "hangup", path: path || "" }) }
  function dial(number) { phonedSend({ cmd: "dial", number: String(number || "") }) }
  function tones(t) { phonedSend({ cmd: "tones", tones: String(t) }) }
  function toggleMute() { phonedSend({ cmd: "mute" }) }
  function swapCalls() { phonedSend({ cmd: "swap" }) }
  function openThread(id) { openThreadId = Number(id) || 0; threadMessages = []; phonedSend({ cmd: "thread", threadId: openThreadId }) }
  function sendSms(threadId, text, addresses) {
    if (!String(text || "").trim()) return
    phonedSend({ cmd: "sms", threadId: Number(threadId) || 0, text: String(text), addresses: addresses || [] })
  }
  function ring() { phonedSend({ cmd: "ring" }) }
  function photos() { phonedSend({ cmd: "photos" }) }
  function sendClipboard() { phonedSend({ cmd: "clipboard" }) }
  // Answer a phone notification in place (WhatsApp, Signal, Telegram…).
  function replyTo(replyId, text, key) { phonedSend({ cmd: "reply", replyId: String(replyId), text: String(text), key: String(key || "") }) }
  // Chats that only exist as notifications (WhatsApp and friends).
  property var chatMessages: []
  property string openChatKey: ""
  readonly property var chats: pstate.chats || []
  function openChat(key) { openChatKey = String(key || ""); chatMessages = []; phonedSend({ cmd: "chat", key: openChatKey }) }
  function pairKdeconnect() { phonedSend({ cmd: "pairKdeconnect" }) }
  function refreshPhone() { phonedSend({ cmd: "refresh" }) }
  // mode: "follow" (default), "pc" to pin audio here, "phone" to leave it there.
  function setAudioMode(mode) { phonedSend({ cmd: "audio", mode: String(mode) }) }

  Process {
    id: phoned
    command: ["python3", root.pluginDir + "/phoned/phoned.py"]
    running: root.runPhoned
    stdinEnabled: true
    stdout: SplitParser {
      onRead: function(line) {
        var msg = null
        try { msg = JSON.parse(line) } catch (e) { return }
        if (msg.type === "state") root.pstate = msg
        else if (msg.type === "thread") { if (msg.threadId === root.openThreadId) root.threadMessages = msg.messages || [] }
        else if (msg.type === "chat") { if (msg.key === root.openChatKey) root.chatMessages = msg.messages || [] }
        else if (msg.type === "event") {
          if (msg.kind === "mute") root.micMuted = !!msg.muted
          if (msg.kind === "error") root.lastError = String(msg.message || "")
          root.phoneEvent(msg)
        }
      }
    }
    onExited: function(code) { if (code !== 75) phonedRestart.restart() }  // 75: a newer phoned took over
  }
  Timer { id: phonedRestart; interval: 3000; onTriggered: phoned.running = root.runPhoned }

  Timer {
    id: refreshTimer
    interval: root.refreshIntervalSec * 1000
    repeat: true
    running: root.poll
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  Timer {
    id: delayedRefresh
    interval: 700
    repeat: false
    onTriggered: root.refresh()
  }

  Timer {
    id: delayedStart
    interval: 900
    repeat: false
    onTriggered: {
      if (root.hasReadyDevice) root.startControl(root.selectedDevice)
      else {
        root._startAfterConnect = false
        root.lastError = "Tailscale connected, but ADB is not authorized yet. Accept the prompt on the phone, then try again."
      }
    }
  }

  Timer {
    id: actionStatusTimer
    interval: 2600
    repeat: false
    onTriggered: root.actionStatus = ""
  }

  Process {
    id: statusProcess
    running: false
    command: []
    stdout: StdioCollector { id: statusStdout; waitForEnd: true; onStreamFinished: root._statusOutput = text }
    stderr: StdioCollector { id: statusStderr; waitForEnd: true; onStreamFinished: root._statusError = text }
    onExited: function(exitCode) {
      root.refreshing = false
      var stdout = String(statusStdout.text || root._statusOutput || "")
      var stderr = String(statusStderr.text || root._statusError || "")
      if (exitCode === 0) root.applyStatus(stdout)
      else {
        root.lastError = root.elideStatus(stderr || stdout || "Could not read phone status")
        root.statusText = root.lastError
      }
      if (root._startAfterRefresh) {
        root.startControl(null)
        root._startAfterRefresh = false
      }
    }
  }

  Process {
    id: actionProcess
    running: false
    command: []
    stdinEnabled: false
    stdout: StdioCollector { id: actionStdout; waitForEnd: true; onStreamFinished: root._actionOutput = text }
    stderr: StdioCollector { id: actionStderr; waitForEnd: true; onStreamFinished: root._actionError = text }
    onStarted: {
      if (root._pendingStdin !== "") {
        var payload = root._pendingStdin
        if (payload.charAt(payload.length - 1) !== "\n") payload += "\n"
        write(payload)
        root._pendingStdin = ""
        stdinEnabled = false
      }
    }
    onExited: function(exitCode) {
      root._pendingStdin = ""
      root.handleActionExit(exitCode)
    }
  }

  Process {
    id: scrcpyProcess
    running: false
    command: []
    stdout: StdioCollector { waitForEnd: false }
    stderr: SplitParser {
      onRead: function(data) {
        var line = String(data || "")
        if (line.toLowerCase().indexOf("error") >= 0 || line.toLowerCase().indexOf("failed") >= 0)
          root._scrcpyError = root.elideStatus(line)
      }
    }
    onExited: function(exitCode) {
      root._desired = -1
      if (exitCode !== 0 && root._scrcpyError)
        root.lastError = root._scrcpyError
      else if (exitCode !== 0)
        root.lastError = "scrcpy exited (" + exitCode + ")"
      root.delayedRefresh.restart()
    }
    onRunningChanged: {
      if (!running && root._desired === 0) root._desired = -1
    }
  }
}
