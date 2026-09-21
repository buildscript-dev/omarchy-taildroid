import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

Panel {
  id: root
  moduleName: "ret.taildroid"
  ipcTarget: "ret.taildroid"
  manageIpc: false

  property string focusSection: "header"
  property int deviceIndex: 0
  property int peerIndex: 0
  property bool cursorActive: false
  property int phraseIndex: 0
  property string pairAddressText: ""
  property string pairCodeText: ""

  readonly property var activePhrases: [
    "Borrowing thumbs",
    "Steering the pocket",
    "Herding pixels",
    "Grabbing the glass",
    "Walking the cursor over",
    "Keeping the phone in the chair"
  ]
  readonly property string heroPhraseText: activePhrases[phraseIndex % activePhrases.length]
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property color iconColor: phone.controlling ? foreground : dim
  readonly property string toggleHint: phone.controlling ? "Release mouse and keyboard" : "Take mouse and keyboard"
  readonly property color barIconColor: phone.controlling ? barForeground : Qt.darker(barForeground, 1.55)
  readonly property bool headerHasCursor: cursorActive && focusSection === "header"
  readonly property bool showDevices: phone.devices.length > 0
  readonly property bool showPeers: phone.tailscalePeers.length > 0
  readonly property bool showBluetooth: phone.bluetoothDevices.length > 0
  readonly property bool pairFieldsFocused: pairAddressField.activeFocus || pairCodeField.activeFocus

  function ensureCursor() {
    if (focusSection === "devices" && !showDevices) focusSection = "header"
    if (focusSection === "peers" && !showPeers) focusSection = showDevices ? "devices" : "header"
    if (deviceIndex >= phone.devices.length) deviceIndex = Math.max(0, phone.devices.length - 1)
    if (peerIndex >= phone.tailscalePeers.length) peerIndex = Math.max(0, phone.tailscalePeers.length - 1)
    if (deviceIndex < 0) deviceIndex = 0
    if (peerIndex < 0) peerIndex = 0
  }

  function moveCursor(dx, dy) {
    cursorActive = true
    ensureCursor()
    if (dy === 0) return
    var order = ["header"]
    if (showDevices) order.push("devices")
    if (showPeers) order.push("peers")
    order.push("tailscale")
    order.push("pair")
    var at = order.indexOf(focusSection)
    if (at < 0) at = 0
    if (focusSection === "devices" && showDevices) {
      var nextDevice = deviceIndex + dy
      if (nextDevice >= 0 && nextDevice < phone.devices.length) {
        deviceIndex = nextDevice
        scrollCursorIntoView()
        return
      }
    }
    if (focusSection === "peers" && showPeers) {
      var nextPeer = peerIndex + dy
      if (nextPeer >= 0 && nextPeer < phone.tailscalePeers.length) {
        peerIndex = nextPeer
        scrollCursorIntoView()
        return
      }
    }
    var next = Math.max(0, Math.min(order.length - 1, at + dy))
    focusSection = order[next]
    if (focusSection === "devices") deviceIndex = dy > 0 ? 0 : Math.max(0, phone.devices.length - 1)
    if (focusSection === "peers") peerIndex = dy > 0 ? 0 : Math.max(0, phone.tailscalePeers.length - 1)
    if (focusSection === "header" && panelFlick) panelFlick.contentY = 0
    scrollCursorIntoView()
  }

  function setHeaderCursor() {
    cursorActive = true
    focusSection = "header"
    if (panelFlick) panelFlick.contentY = 0
  }

  function activateCursor() {
    ensureCursor()
    if (focusSection === "header") phone.toggleControl()
    else if (focusSection === "devices") phone.startControl(selectedDevice())
    else if (focusSection === "peers") connectPeer(selectedPeer())
    else if (focusSection === "tailscale") phone.connectTailscale()
    else if (focusSection === "pair") phone.pair(pairAddressText, pairCodeText)
  }

  function rowAt(column, index) {
    if (!column) return null
    var rows = []
    for (var i = 0; i < column.children.length; i++) {
      var child = column.children[i]
      if (child && child.rowIndex !== undefined) rows.push(child)
    }
    return index >= 0 && index < rows.length ? rows[index] : null
  }

  function selectedDevice() {
    if (phone.devices.length === 0) return null
    return phone.devices[Math.max(0, Math.min(deviceIndex, phone.devices.length - 1))]
  }

  function selectedPeer() {
    if (phone.tailscalePeers.length === 0) return null
    return phone.tailscalePeers[Math.max(0, Math.min(peerIndex, phone.tailscalePeers.length - 1))]
  }

  function connectPeer(peer) {
    if (!peer || !peer.ip) return
    phone.connectAddress(peer.ip + ":" + phone.adbPort)
  }

  function setDeviceCursor(index) {
    cursorActive = true
    focusSection = "devices"
    deviceIndex = index
    scrollCursorIntoView()
  }

  function setPeerCursor(index) {
    cursorActive = true
    focusSection = "peers"
    peerIndex = index
    scrollCursorIntoView()
  }

  function scrollItemIntoView(item) {
    if (!panelFlick || !item) return
    Qt.callLater(function() {
      if (!item) return
      var margin = Style.space(6)
      var point = item.mapToItem(panelFlick.contentItem, 0, 0)
      var top = point.y
      var bottom = top + item.height
      var viewTop = panelFlick.contentY
      var viewBottom = viewTop + panelFlick.height
      var maxY = Math.max(0, panelFlick.contentHeight - panelFlick.height)
      if (top < viewTop + margin) panelFlick.contentY = Math.max(0, top - margin)
      else if (bottom > viewBottom - margin) panelFlick.contentY = Math.min(maxY, bottom + margin - panelFlick.height)
    })
  }

  function scrollCursorIntoView() {
    if (focusSection === "devices") scrollItemIntoView(rowAt(deviceColumn, deviceIndex))
    else if (focusSection === "peers") scrollItemIntoView(rowAt(peerColumn, peerIndex))
    else if (focusSection === "tailscale") scrollItemIntoView(tailscaleButton)
    else if (focusSection === "pair") scrollItemIntoView(pairButton)
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onOpenedChanged: if (opened) {
    cursorActive = false
    if (panelFlick) panelFlick.contentY = 0
    if (pairAddressText === "")
      pairAddressText = String(Model.setting(settings, "pairAddress", ""))
    phone.refresh()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  Service {
    id: phone
    settings: root.settings
  }

  Connections {
    target: phone
    function onDevicesChanged() { root.ensureCursor() }
    function onTailscalePeersChanged() { root.ensureCursor() }
  }

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { phone.refresh(); return "ok" }
    function toggleControl(): string { phone.toggleControl(); return phone.statusText }
    function start(): string { phone.startControl(phone.selectedDevice); return phone.statusText }
    function stop(): string { phone.stopControl(); return "ok" }
    function connectTailscale(): string { phone.connectTailscale(); return "ok" }
    function pair(addr: string, code: string): string { phone.pair(addr, code); return "ok" }
    function status(): string { return phone.lastError !== "" ? phone.lastError : phone.statusText }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    tooltipText: phone.controlling ? "Release Taildroid" : "Taildroid"
    iconComponent: Component {
      Item {
        PhoneIcon {
          anchors.centerIn: parent
          iconSize: Style.space(12)
          color: root.barIconColor
          active: phone.controlling
        }
      }
    }
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) phone.toggleControl()
      else if (buttonCode === Qt.MiddleButton) phone.refresh()
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(400))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(620))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: root.pairFieldsFocused
      onMoveRequested: function(dx, dy) {
        if (!root.cursorActive) { root.cursorActive = true; return }
        root.moveCursor(dx, dy)
      }
      onActivateRequested: if (root.cursorActive) root.activateCursor()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        if (t === "r" || t === "R") phone.refresh()
        else if (t === "c" || t === "C") phone.connectTailscale()
        else if (t === "p" || t === "P") phone.pair(root.pairAddressText, root.pairCodeText)
      }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          width: panelFlick.width
          spacing: Style.space(12)

          Item {
            id: header
            width: parent.width
            implicitHeight: hero.implicitHeight
            readonly property bool ringVisible: root.headerHasCursor
            function focusHero() { root.setHeaderCursor() }

            PanelHero {
              id: hero
              width: parent.width
              title: phone.selectedDevice && phone.selectedDevice.name ? phone.selectedDevice.name : "Taildroid"
              meta: phone.controlling ? root.heroPhraseText : phone.statusText
              foreground: root.foreground
              fontFamily: root.fontFamily
              iconOpacity: phone.controlling ? 1.0 : 0.55
              iconComponent: Component {
                PhoneIcon {
                  iconSize: Style.font.display
                  color: root.iconColor
                  active: phone.controlling
                }
              }
              trailingControl: Component {
                ToggleSwitch {
                  id: powerSwitch
                  visible: phone.adbInstalled && phone.scrcpyInstalled
                  checked: phone.controlling
                  busy: phone.busy
                  hasCursor: header.ringVisible
                  foreground: hero.foreground
                  onHovered: function(on) { if (on) header.focusHero() }
                  onToggled: phone.toggleControl()

                  PanelToolTip {
                    visible: powerSwitch.containsMouse
                    text: root.toggleHint
                    fontFamily: hero.fontFamily
                  }
                }
              }
            }
          }

          Text {
            textFormat: Text.PlainText
            visible: phone.actionStatus !== "" || phone.lastError !== ""
            width: parent.width
            text: phone.actionStatus !== "" ? phone.actionStatus : phone.lastError
            color: phone.lastError !== "" && phone.actionStatus === "" ? root.urgent : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          Column {
            visible: !phone.adbInstalled || !phone.scrcpyInstalled
            width: parent.width
            spacing: Style.space(6)
            Text {
              width: parent.width
              text: phone.missingToolsText()
              color: root.urgent
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              wrapMode: Text.WordWrap
            }
          }

          Column {
            visible: phone.adbInstalled && phone.scrcpyInstalled && !phone.hasReadyDevice
            width: parent.width
            spacing: Style.space(6)
            Text {
              width: parent.width
              text: "Bluetooth is audio only. Control needs Wireless debugging, or USB once plus adb tcpip 5555, then ADB over Tailscale or Wi-Fi."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              wrapMode: Text.WordWrap
            }
            Text {
              width: parent.width
              text: "On the phone: Settings → System → Developer options → Wireless debugging. Pair with the fields below, or connect USB, run adb tcpip 5555, then Super+Shift+I."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              wrapMode: Text.WordWrap
            }
          }

          ActionRow {
            id: tailscaleButton
            visible: phone.adbInstalled
            title: phone.onlinePeer ? "Connect " + phone.onlinePeer.name + " over Tailscale" : "Connect over Tailscale"
            subtitle: phone.onlinePeer
              ? Model.maskHostPort(String(phone.onlinePeer.ip) + ":" + phone.adbPort)
              : "No online Android Tailscale peer"
            enabled: !!(phone.onlinePeer && phone.onlinePeer.ip)
            hasCursor: root.cursorActive && root.focusSection === "tailscale"
            onHover: { root.cursorActive = true; root.focusSection = "tailscale" }
            onActivate: phone.connectTailscale()
          }

          Column {
            id: pairButton
            visible: phone.adbInstalled
            width: parent.width
            spacing: Style.space(6)

            PanelSectionHeader {
              text: "WIRELESS PAIRING"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            TextField {
              id: pairAddressField
              width: parent.width
              foreground: root.foreground
              placeholderText: "host:port from the pairing popup"
              text: root.pairAddressText
              onTextChanged: root.pairAddressText = text
              onAccepted: pairCodeField.forceActiveFocus()
              Keys.onPressed: function(event) {
                if (event.key === Qt.Key_Escape) {
                  keyCatcher.forceActiveFocus()
                  event.accepted = true
                }
              }
            }

            TextField {
              id: pairCodeField
              width: parent.width
              foreground: root.foreground
              placeholderText: "Six-digit pairing code"
              text: root.pairCodeText
              password: true
              inputMethodHints: Qt.ImhDigitsOnly
              onTextChanged: root.pairCodeText = text
              onAccepted: phone.pair(root.pairAddressText, root.pairCodeText)
              Keys.onPressed: function(event) {
                if (event.key === Qt.Key_Escape) {
                  keyCatcher.forceActiveFocus()
                  event.accepted = true
                }
              }
            }

            ActionRow {
              width: parent.width
              title: "Pair wireless debugging"
              subtitle: "Codes expire in about two minutes and are not saved"
              enabled: root.pairAddressText !== "" && root.pairCodeText !== ""
              hasCursor: root.cursorActive && root.focusSection === "pair"
              onHover: { root.cursorActive = true; root.focusSection = "pair" }
              onActivate: phone.pair(root.pairAddressText, root.pairCodeText)
            }
          }

          PanelSeparator {
            visible: root.showDevices
            foreground: root.foreground
          }

          Column {
            visible: root.showDevices
            width: parent.width
            spacing: Style.space(10)

            PanelSectionHeader {
              text: "ADB DEVICES"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Column {
              id: deviceColumn
              width: parent.width
              spacing: Style.space(6)

              Repeater {
                model: phone.devices
                DeviceRow {
                  required property var modelData
                  required property int index
                  width: deviceColumn.width
                  device: modelData
                  rowIndex: index
                }
              }
            }
          }

          PanelSeparator {
            visible: root.showPeers
            foreground: root.foreground
          }

          Column {
            visible: root.showPeers
            width: parent.width
            spacing: Style.space(10)

            PanelSectionHeader {
              text: "TAILSCALE"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Column {
              id: peerColumn
              width: parent.width
              spacing: Style.space(6)

              Repeater {
                model: phone.tailscalePeers
                PeerRow {
                  required property var modelData
                  required property int index
                  width: peerColumn.width
                  peer: modelData
                  rowIndex: index
                }
              }
            }
          }

          PanelSeparator {
            visible: root.showBluetooth
            foreground: root.foreground
          }

          Column {
            visible: root.showBluetooth
            width: parent.width
            spacing: Style.spacing.labelGap

            PanelSectionHeader {
              text: "BLUETOOTH"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Repeater {
              model: phone.bluetoothDevices
              InfoPair {
                required property var modelData
                width: column.width
                label: modelData.name || "Phone"
                value: "Connected · audio only"
              }
            }
          }

          Text {
            width: parent.width
            text: "UHID captures the pointer. Press Left Alt or Super to give the mouse back to the desktop."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }
        }
      }
    }
  }

  Timer {
    id: phraseTimer
    interval: 2800
    running: root.opened && phone.controlling
    repeat: true
    onTriggered: phraseSwap.restart()
  }

  SequentialAnimation {
    id: phraseSwap
    PropertyAnimation {
      target: hero; property: "metaOpacity"
      to: 0.0; duration: 180; easing.type: Easing.OutQuad
    }
    ScriptAction {
      script: root.phraseIndex = (root.phraseIndex + 1) % root.activePhrases.length
    }
    PropertyAnimation {
      target: hero; property: "metaOpacity"
      to: 1.0; duration: 260; easing.type: Easing.InQuad
    }
  }

  component ActionRow: CursorSurface {
    id: actionRow
    property string title: ""
    property string subtitle: ""
    property bool enabled: true
    signal activate()
    signal hover()

    implicitHeight: actionContent.implicitHeight + Style.spacing.rowPaddingX

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: actionRow.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
      enabled: actionRow.enabled
      onEntered: actionRow.hover()
      onClicked: actionRow.activate()
    }

    RowLayout {
      id: actionContent
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(8)

      ColumnLayout {
        Layout.fillWidth: true
        spacing: Style.space(1)
        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: actionRow.title
          color: root.foreground
          opacity: actionRow.enabled ? 1 : 0.5
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
        }
        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: actionRow.subtitle
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }
      }
    }
  }

  component DeviceRow: CursorSurface {
    id: deviceRow
    property var device: null
    property int rowIndex: 0
    readonly property bool ready: device && device.state === "device"

    hasCursor: root.cursorActive && root.focusSection === "devices" && root.deviceIndex === rowIndex
    foreground: root.foreground
    implicitHeight: deviceContent.implicitHeight + Style.spacing.rowPaddingX

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: deviceRow.ready ? Qt.PointingHandCursor : Qt.ArrowCursor
      onEntered: root.setDeviceCursor(deviceRow.rowIndex)
      onClicked: if (deviceRow.ready) phone.startControl(deviceRow.device)
    }

    RowLayout {
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(8)

      PhoneIcon {
        iconSize: Style.font.icon
        color: deviceRow.ready ? root.foreground : root.dim
        active: phone.controlling && phone.controllingSerial === (deviceRow.device ? deviceRow.device.serial : "")
        Layout.alignment: Qt.AlignVCenter
      }

      ColumnLayout {
        id: deviceContent
        Layout.fillWidth: true
        spacing: Style.space(1)
        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: Model.displayDeviceName(deviceRow.device)
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
        }
        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: Model.deviceSubtitle(deviceRow.device)
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }
    }
  }

  component PeerRow: CursorSurface {
    id: peerRow
    property var peer: null
    property int rowIndex: 0
    readonly property bool ready: peer && peer.online && peer.ip

    hasCursor: root.cursorActive && root.focusSection === "peers" && root.peerIndex === rowIndex
    foreground: root.foreground
    implicitHeight: peerContent.implicitHeight + Style.spacing.rowPaddingX

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: peerRow.ready ? Qt.PointingHandCursor : Qt.ArrowCursor
      onEntered: root.setPeerCursor(peerRow.rowIndex)
      onClicked: if (peerRow.ready) root.connectPeer(peerRow.peer)
    }

    RowLayout {
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(8)

      ColumnLayout {
        id: peerContent
        Layout.fillWidth: true
        spacing: Style.space(1)
        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: peerRow.peer ? peerRow.peer.name : "Android"
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
        }
        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: Model.peerSubtitle(peerRow.peer)
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }
    }
  }

  component InfoPair: Row {
    property string label: ""
    property string value: ""
    width: parent.width
    spacing: Style.space(8)
    Text {
      textFormat: Text.PlainText
      text: label
      color: root.foreground
      opacity: 0.6
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }
    Item { width: Math.max(0, parent.width - parent.children[0].implicitWidth - parent.children[2].implicitWidth - parent.spacing * 2); height: 1 }
    Text {
      textFormat: Text.PlainText
      text: value
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      elide: Text.ElideRight
    }
  }
}
