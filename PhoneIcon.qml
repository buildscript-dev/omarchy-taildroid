import QtQuick
import qs.Commons

Item {
  id: root

  property real iconSize: Style.font.icon
  property color color: Color.foreground
  property bool active: false

  width: iconSize
  height: iconSize
  implicitWidth: iconSize
  implicitHeight: iconSize

  readonly property real bodyW: iconSize * 0.56
  readonly property real bodyH: iconSize * 0.92
  readonly property real stroke: Math.max(1.4, iconSize * 0.09)

  Rectangle {
    id: body
    anchors.centerIn: parent
    width: root.bodyW
    height: root.bodyH
    radius: width * 0.22
    color: "transparent"
    border.color: root.color
    border.width: root.stroke
  }

  Rectangle {
    width: root.bodyW * 0.32
    height: Math.max(1.5, root.iconSize * 0.07)
    radius: height / 2
    color: root.color
    anchors.horizontalCenter: body.horizontalCenter
    anchors.top: body.top
    anchors.topMargin: root.iconSize * 0.14
  }

  Rectangle {
    visible: root.active
    width: Math.max(3, root.iconSize * 0.18)
    height: width
    radius: width / 2
    color: root.color
    anchors.right: parent.right
    anchors.bottom: parent.bottom
    anchors.rightMargin: -1
    anchors.bottomMargin: -1
  }
}
