import QtQuick
import QtQuick.Window
import QtQuick.Effects
import QtMultimedia

// Phone window: a Galaxy-style body (or an official Samsung emulator skin)
// with the live screen inside, like iPhone Mirroring on a Mac.
Window {
  id: win
  visible: true
  color: "transparent"
  flags: Qt.Window | Qt.FramelessWindowHint
  title: windowTitle || ("Taildroid — " + (session.deviceName || "Phone"))

  readonly property bool hasSkin: !!(skin && skin.image)
  readonly property bool bare: frameStyle === "none"
  readonly property bool exporting: exportDir !== ""
  readonly property int fw: session.frameWidth > 0 ? session.frameWidth : (hasSkin ? skin.screenW : 1080)
  readonly property int fh: session.frameHeight > 0 ? session.frameHeight : (hasSkin ? skin.screenH : 2340)
  readonly property bool landscape: fw > fh
  readonly property real shortSide: Math.min(fw, fh)

  // Built-in bodies, in fractions of the screen's short side (1080 px).
  // s24: Galaxy S24 — 70.6 × 147.0 mm body, 6.2" 1080×2340 panel (≈16.4 px/mm):
  //   2.35 mm side / 2.05 mm top-bottom bezels, rounded corners, flat
  //   Armor Aluminum sides, centered punch-hole, keys on the right.
  // s24-ultra: the first built-in frame, kept exactly as it was.
  readonly property var profiles: ({
    "s24": { bezelX: 0.0357, bezelY: 0.0311, rim: 0.0106, screenRadius: 0.100, bodyExtra: 1.05,
             hole: 0.042, holeY: 0.022, flat: true, color: "onyx-black",
             keys: [{ from: 0.215, to: 0.290, code: 24 }, { from: 0.290, to: 0.365, code: 25 }, { from: 0.430, to: 0.505, code: 26 }] },
    "s24-ultra": { bezelX: 0.028, bezelY: 0.028, rim: 0.012, screenRadius: 0.100, bodyExtra: 1.0,
                   hole: 0.034, holeY: 0.028, flat: false, color: "#3a3c42",
                   keys: [{ from: 0.20, to: 0.31, code: 24 }, { from: 0.31, to: 0.42, code: 25 }, { from: 0.47, to: 0.56, code: 26 }] }
  })
  // Official finishes; any #hex also works.
  readonly property var finishes: ({
    "onyx-black": "#2e2f33", "marble-grey": "#c9c7c4", "cobalt-violet": "#8b7fb8", "amber-yellow": "#e6d5a3",
    "jade-green": "#b5c9b0", "sandstone-orange": "#e7b28f", "sapphire-blue": "#6f8fc2",
    "titanium-black": "#3a3a3c", "titanium-gray": "#8b8a87", "titanium-violet": "#7c7688", "titanium-yellow": "#d9ceb0"
  })
  readonly property var prof: profiles[frameStyle] || profiles["s24"]
  readonly property color metal: {
    var c = frameColor || prof.color
    return finishes[c] || c
  }
  readonly property bool lightMetal: metal.hslLightness > 0.55

  // Geometry in device pixels; everything is scaled by `s` to fit the window.
  readonly property bool useSkin: hasSkin && !bare
  readonly property real rim: bare ? 0 : shortSide * prof.rim
  readonly property real bezelX: bare ? 0 : shortSide * (landscape ? prof.bezelY : prof.bezelX)
  readonly property real bezelY: bare ? 0 : shortSide * (landscape ? prof.bezelX : prof.bezelY)
  readonly property real bezel: Math.max(bezelX, bezelY)
  readonly property real screenRadius: bare ? 0 : shortSide * (hasSkin ? 0.075 : prof.screenRadius)
  readonly property real bodyW: useSkin ? (landscape ? skin.height : skin.width) : fw + 2 * bezelX
  readonly property real bodyH: useSkin ? (landscape ? skin.width : skin.height) : fh + 2 * bezelY
  readonly property rect screenRect: useSkin
      ? (landscape ? Qt.rect(skin.screenY, skin.width - skin.screenX - skin.screenW, skin.screenH, skin.screenW)
                   : Qt.rect(skin.screenX, skin.screenY, skin.screenW, skin.screenH))
      : Qt.rect(bezelX, bezelY, fw, fh)

  // Export renders the frame 1:1 with an 8 px margin for the side keys.
  readonly property real topBar: bare || exporting ? 0 : 52
  readonly property real pad: bare ? 0 : exporting ? 8 : 28
  readonly property real s: exporting ? 1 : Math.min((width - 2 * pad) / bodyW, (height - topBar - 2 * pad) / bodyH)

  function fitSize() {
    if (exporting) {
      width = Math.ceil(bodyW + 2 * pad)
      height = Math.ceil(bodyH + 2 * pad)
      return
    }
    var h = Screen.height * (bare ? 0.7 : 0.86)
    var scale = (h - topBar - 2 * pad) / bodyH
    var w = Math.round(bodyW * scale + 2 * pad)
    if (landscape) {
      // Same phone size turned sideways: the long side stays the same length.
      w = Math.round(h)
      h = Math.round((w - 2 * pad) * bodyH / bodyW + topBar + 2 * pad)
    }
    width = w
    height = Math.round(h)
    if (visible) hypr.resize(width, height)
  }
  Component.onCompleted: {
    fitSize()
    if (exporting) exportTimer.start()
  }
  onLandscapeChanged: fitSize()
  onActiveChanged: if (!active) session.releaseKeys()

  // Writes device_Port.png + layout (Android emulator skin format), then quits.
  Timer {
    id: exportTimer
    interval: 600
    onTriggered: win.contentItem.grabToImage(function(result) {
      result.saveToFile(exportDir + "/device_Port.png")
      exporter.writeLayout(exportDir, Math.ceil(win.bodyW + 2 * win.pad), Math.ceil(win.bodyH + 2 * win.pad),
                           Math.round(win.pad + win.bezelX), Math.round(win.pad + win.bezelY), win.fw, win.fh)
      Qt.quit()
    })
  }

  // ---------------------------------------------------------------- keyboard
  Item {
    id: keys
    focus: true
    Keys.onPressed: function(e) { if (!e.isAutoRepeat) session.key(e.nativeScanCode, true); e.accepted = true }
    Keys.onReleased: function(e) { if (!e.isAutoRepeat) session.key(e.nativeScanCode, false); e.accepted = true }
  }

  HoverHandler { id: hover }

  // ---------------------------------------------------------------- toolbar
  Rectangle {
    id: toolbar
    visible: !win.bare && !win.exporting
    anchors.horizontalCenter: parent.horizontalCenter
    y: 8
    height: 36
    width: row.implicitWidth + 16
    radius: height / 2
    color: Qt.rgba(0.08, 0.08, 0.09, 0.92)
    border.color: Qt.rgba(1, 1, 1, 0.08)
    opacity: hover.hovered ? 1 : 0
    Behavior on opacity { NumberAnimation { duration: 180 } }

    DragHandler { target: null; onActiveChanged: if (active) win.startSystemMove() }

    Row {
      id: row
      anchors.centerIn: parent
      spacing: 2
      Repeater {
        model: [
          { glyph: "\u{F0141}", tip: "Back", act: function() { session.backOrScreenOn() } },
          { glyph: "\u{F02DC}", tip: "Home", act: function() { session.keycode(3) } },
          { glyph: "\u{F0131}", tip: "Recent apps", act: function() { session.keycode(187) } },
          { glyph: "\u{F009A}", tip: "Notifications", act: function() { session.expandNotifications() } },
          { glyph: "\u{F0467}", tip: "Rotate", act: function() { session.rotate() } },
          { glyph: "\u{F0425}", tip: "Phone screen on/off", act: function() { session.setScreenPower(!session.screenOn) } },
          { glyph: "\u{F0156}", tip: "Close", act: function() { Qt.quit() } }
        ]
        delegate: Rectangle {
          required property var modelData
          width: 32; height: 28; radius: 14
          color: tap.containsMouse ? Qt.rgba(1, 1, 1, 0.12) : "transparent"
          Text {
            anchors.centerIn: parent
            text: modelData.glyph
            font.family: "JetBrainsMono Nerd Font"
            font.pixelSize: 17
            color: "#e8e8ea"
          }
          MouseArea { id: tap; anchors.fill: parent; hoverEnabled: true; onClicked: modelData.act() }
        }
      }
    }
  }

  // ---------------------------------------------------------------- phone
  Item {
    id: phone
    width: win.bodyW * win.s
    height: win.bodyH * win.s
    x: (win.width - width) / 2
    y: win.topBar + (win.height - win.topBar - height) / 2

    DragHandler { target: null; onActiveChanged: if (active) win.startSystemMove() }

    // Built-in Galaxy body: brushed-metal rim, black glass bezel.
    Rectangle {
      id: body
      visible: !win.useSkin && !win.bare
      anchors.fill: parent
      radius: (win.screenRadius + win.bezel * win.prof.bodyExtra) * win.s
      // Flat aluminum sides catch light as a horizontal band; the old frame
      // used a top-to-bottom sheen.
      gradient: Gradient {
        orientation: win.prof.flat ? Gradient.Horizontal : Gradient.Vertical
        GradientStop { position: 0; color: win.prof.flat ? Qt.darker(win.metal, 1.12) : Qt.lighter(win.metal, 1.35) }
        GradientStop { position: win.prof.flat ? 0.08 : 0.5; color: win.prof.flat ? Qt.lighter(win.metal, 1.22) : win.metal }
        GradientStop { position: win.prof.flat ? 0.5 : 1; color: win.prof.flat ? win.metal : Qt.darker(win.metal, 1.25) }
        GradientStop { position: win.prof.flat ? 0.92 : 1; color: win.prof.flat ? Qt.lighter(win.metal, 1.22) : Qt.darker(win.metal, 1.25) }
        GradientStop { position: 1; color: win.prof.flat ? Qt.darker(win.metal, 1.12) : Qt.darker(win.metal, 1.25) }
      }
      border.width: 1
      border.color: win.lightMetal ? Qt.rgba(0, 0, 0, 0.18) : Qt.rgba(1, 1, 1, 0.18)
      layer.enabled: !win.exporting
      layer.effect: MultiEffect {
        shadowEnabled: true
        shadowBlur: 1.0
        shadowOpacity: 0.55
        shadowVerticalOffset: 10
        shadowColor: "black"
      }

      Rectangle {
        anchors.fill: parent
        anchors.margins: win.rim * win.s
        radius: parent.radius - anchors.margins
        color: "#040405"
      }
    }

    // Side keys (right edge in portrait, top edge in landscape).
    Repeater {
      model: win.useSkin || win.bare ? [] : win.prof.keys  // volume up, volume down, side key
      delegate: Rectangle {
        required property var modelData
        readonly property real len: (win.landscape ? phone.width : phone.height) * (modelData.to - modelData.from) - 3
        readonly property real at: (win.landscape ? phone.width * (1 - modelData.to) : phone.height * modelData.from)
        x: win.landscape ? at : phone.width - 1
        y: win.landscape ? -3 : at
        width: win.landscape ? len : 4
        height: win.landscape ? 4 : len
        radius: 2
        color: keyTap.pressed ? Qt.darker(win.metal, 1.4) : Qt.lighter(win.metal, 1.15)
        z: -1
        MouseArea {
          id: keyTap
          anchors.fill: parent
          anchors.margins: -4
          onClicked: session.keycode(modelData.code)
        }
      }
    }

    // Official Samsung emulator skin, rotated with the device.
    Image {
      visible: win.useSkin
      source: win.useSkin ? skin.image : ""
      width: win.useSkin ? skin.width * win.s : 0
      height: win.useSkin ? skin.height * win.s : 0
      anchors.centerIn: parent
      rotation: win.landscape ? -90 : 0
      smooth: true
      mipmap: true
      layer.enabled: win.useSkin
      layer.effect: MultiEffect {
        shadowEnabled: true
        shadowBlur: 1.0
        shadowOpacity: 0.55
        shadowVerticalOffset: 10
        shadowColor: "black"
      }
    }

    // ------------------------------------------------------------ screen
    Item {
      id: screen
      x: win.screenRect.x * win.s
      y: win.screenRect.y * win.s
      width: win.screenRect.width * win.s
      height: win.screenRect.height * win.s

      Item {
        id: glass
        visible: !win.exporting
        anchors.fill: parent
        layer.enabled: win.screenRadius > 0
        layer.smooth: true
        layer.effect: MultiEffect {
          maskEnabled: true
          maskSource: mask
          maskThresholdMin: 0.5
          maskSpreadAtMin: 1.0
        }

        Rectangle { anchors.fill: parent; color: "black" }

        VideoOutput {
          id: video
          anchors.fill: parent
          fillMode: VideoOutput.Stretch
          Component.onCompleted: session.videoSink = video.videoSink
        }

        // Connecting / error card.
        Rectangle {
          anchors.fill: parent
          color: "black"
          visible: session.state !== "streaming" || session.frameWidth === 0
          Column {
            anchors.centerIn: parent
            width: parent.width * 0.8
            spacing: 14
            Text {
              width: parent.width
              horizontalAlignment: Text.AlignHCenter
              text: session.deviceName || "Galaxy"
              color: "white"
              font.pixelSize: Math.max(14, screen.width * 0.07)
              font.weight: Font.DemiBold
            }
            Text {
              width: parent.width
              horizontalAlignment: Text.AlignHCenter
              wrapMode: Text.WordWrap
              text: session.state === "error" && session.error === "Phone disconnected" ? "Reconnecting…"
                  : session.state === "error" ? session.error
                  : session.state === "closed" ? "Disconnected" : "Connecting…"
              color: session.state === "error" ? "#ff8a80" : "#9a9aa0"
              font.pixelSize: Math.max(11, screen.width * 0.04)
            }
          }
        }
      }

      Rectangle {
        id: mask
        anchors.fill: parent
        radius: win.screenRadius * win.s
        visible: false
        layer.enabled: true
        layer.smooth: true
      }

      // Punch-hole camera (skins draw their own).
      Rectangle {
        visible: !win.bare && !win.useSkin && !win.exporting
        readonly property real d: Math.min(screen.width, screen.height) * win.prof.hole
        width: d; height: d; radius: d / 2
        color: "#020203"
        border.color: "#15161a"
        border.width: Math.max(1, d * 0.12)
        x: win.landscape ? Math.min(screen.width, screen.height) * win.prof.holeY : (screen.width - d) / 2
        y: win.landscape ? (screen.height - d) / 2 : Math.min(screen.width, screen.height) * win.prof.holeY
        Rectangle {
          width: parent.d * 0.28; height: width; radius: width / 2
          x: parent.d * 0.22; y: parent.d * 0.22
          color: Qt.rgba(0.35, 0.4, 0.6, 0.35)
        }
      }

      // ---------------------------------------------------------- input
      MouseArea {
        anchors.fill: parent
        acceptedButtons: Qt.LeftButton | Qt.RightButton | Qt.MiddleButton
        preventStealing: true
        function vx(m) { return Math.max(0, Math.min(win.fw - 1, m.x / width * win.fw)) }
        function vy(m) { return Math.max(0, Math.min(win.fh - 1, m.y / height * win.fh)) }
        onPressed: function(m) {
          keys.forceActiveFocus()
          if (m.button === Qt.LeftButton) session.touch(0, vx(m), vy(m))
          else if (m.button === Qt.RightButton) session.backOrScreenOn()
          else session.keycode(3)
        }
        onPositionChanged: function(m) { if (pressedButtons & Qt.LeftButton) session.touch(2, vx(m), vy(m)) }
        onReleased: function(m) { if (m.button === Qt.LeftButton) session.touch(1, vx(m), vy(m)) }
        onWheel: function(w) {
          session.scroll(vx(w), vy(w), -w.angleDelta.x / 120, w.angleDelta.y / 120)
        }
      }
    }
  }
}
