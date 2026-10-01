// Trello icon with the number of your cards due within a day; click opens the client.
// The count comes from `trello due --cached` (kept fresh by the app and the snapshot timer),
// so reading it never touches the network.

import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "petrzpav.trello"

  readonly property string script: Qt.resolvedUrl("bin/trello").toString().replace(/^file:\/\//, "")
  readonly property bool hideWhenNothingDue: setting("hideWhenNothingDue", true) === true
  property int due: 0

  visible: !(hideWhenNothingDue && due === 0)
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function refresh() {
    if (!countProc.running) countProc.running = true
  }

  function open() {
    if (root.bar) root.bar.run(script + "-window")
  }

  IpcHandler {
    target: "petrzpav.trello"
    function refresh(): void { root.broadcast("refresh") }
  }

  Process {
    id: countProc
    command: [root.script, "due", "--cached"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var n = parseInt(text.trim())
        if (!isNaN(n)) root.due = n
      }
    }
  }

  Timer {
    interval: 60000
    running: true
    repeat: true
    onTriggered: root.refresh()
  }

  Component.onCompleted: refresh()

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.due > 0 ? " " + root.due : ""
    active: root.due > 0
    fontSize: Style.font.caption
    horizontalMargin: 6
    tooltipText: root.due > 0 ? root.due + " of your cards due within a day" : "Trello"
    onPressed: root.open()
  }
}
