import QtQuick
import QtQuick.Controls
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "community.omafanslegion"
  ipcTarget: "community.omafanslegion"
  manageIpc: false

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  property var service: null
  function resolveService() {
    if (bar && bar.shell && typeof bar.shell.serviceFor === "function") {
      var found = bar.shell.serviceFor(moduleName)
      if (found !== service) service = found
    }
  }
  onBarChanged: resolveService()
  Timer {
    interval: 1000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.resolveService()
  }

  readonly property bool available: service ? service.available : false
  readonly property bool serviceUp: service ? service.serviceUp : false
  readonly property bool controlEnabled: service ? service.controlEnabled : false
  readonly property string mode: service ? service.mode : "auto"
  readonly property var fans: service ? service.fans : []
  readonly property real temperature: service ? service.temperature : 0
  readonly property string hottestZone: service ? service.hottestZone : ""
  readonly property real hottestCelsius: service ? service.hottestCelsius : 0
  readonly property string statusError: service ? service.error : ""
  readonly property string notice: service ? service.notice : ""
  readonly property string helperPath: decodeURIComponent(Qt.resolvedUrl("fanctl.py").toString().replace(/^file:\/\//, ""))
  readonly property bool busy: service ? service.busy : false
  readonly property bool canControl: controlEnabled && !busy

  // The bar instance resolves the shared service object through the shell's
  // bar facade; when that lookup fails (bar built before the service
  // registered, or a facade without service access) fall back to polling the
  // unprivileged helper directly so the bar label never shows less than the
  // widget itself knows.
  property bool fallbackActive: false
  property var fallbackFans: []
  property real fallbackTemp: 0
  readonly property var effectiveFans: (service && service.fans) ? service.fans : fallbackFans
  readonly property real effectiveTemp: (service && service.temperature) ? service.temperature : fallbackTemp
  readonly property bool effectiveServiceUp: service ? service.serviceUp : (fallbackRan && !fallbackFailed)

  property bool fallbackRan: false
  property bool fallbackFailed: false

  function startFallback() {
    if (service) return
    fallbackActive = true
    fallbackTimer.running = true
  }

  Timer {
    id: fallbackTimer
    interval: 5000
    repeat: true
    running: false
    triggeredOnStart: true
    onTriggered: {
      if (root.service) { running = false; return }
      if (fallbackHelper.running) return
      fallbackHelper.command = ["/usr/bin/python3", root.helperPath, "status"]
      fallbackHelper.running = true
    }
  }

  Process {
    id: fallbackHelper
    stdout: StdioCollector { id: fallbackOutput; waitForEnd: true }
    onExited: function(exitCode) {
      if (exitCode !== 0 || !fallbackOutput.text) { root.fallbackFailed = true; return }
      var parsed = null
      try { parsed = JSON.parse(String(fallbackOutput.text)) } catch (e) { parsed = null }
      if (!parsed || typeof parsed !== "object") { root.fallbackFailed = true; return }
      root.fallbackRan = true
      root.fallbackFans = Array.isArray(parsed.fans) ? parsed.fans : []
      root.fallbackTemp = typeof parsed.temperature === "number" ? parsed.temperature : 0
    }
  }

  onServiceChanged: {
    if (service) {
      fallbackActive = false
      fallbackTimer.running = false
      fallbackFans = []
      fallbackRan = false
      fallbackFailed = false
    } else {
      startFallback()
    }
  }

  // RPM range the daemon accepts
  readonly property int rpmMin: 1200
  readonly property int rpmMax: 5300
  readonly property int rpmStep: 100

  property int manualDraft: 3000
  property bool manualDraftTouched: false
  property var curveDraft: [[45, 1400], [60, 2400], [72, 3400], [82, 4400], [92, 5200]]
  property bool curveDraftTouched: false

  function rpmText() {
    var source = (service && service.fans) ? service.fans : fallbackFans
    var total = 0, count = 0
    for (var i = 0; i < source.length; i++) {
      var f = source[i]
      if (typeof f.rpm === "number" && f.rpm > 0) { total += f.rpm; count++ }
    }
    if (count === 0) return "—"
    return Math.round(total / count) + " RPM"
  }

  function tempText() {
    var t = (service && service.temperature) ? service.temperature : fallbackTemp
    return t > 0 ? Math.round(t) + "°C" : "—"
  }

  function barLabel() {
    if (!effectiveServiceUp && !available) return "Fan —"
    if (effectiveFans.length > 0 && typeof effectiveFans[0].rpm === "number")
      return "Fan " + Math.round(effectiveFans[0].rpm)
    return "Fan Auto"
  }

  function barTooltip() {
    var lines = ["LegionFan · " + mode.toUpperCase()]
    lines.push(rpmText() + " · CPU " + tempText())
    if (hottestCelsius > 0) lines.push("Hot: " + hottestZone + " " + Math.round(hottestCelsius) + "°C")
    if (statusError !== "") lines.push(statusError)
    else if (notice !== "") lines.push(notice)
    return lines.join("\n")
  }

  function seedDrafts() {
    if (!service) return
    if (!manualDraftTouched) manualDraft = service.manualRpm
    if (!curveDraftTouched) curveDraft = service.curve
  }

  Connections {
    target: service
    function onManualRpmChanged() {
      if (root.manualDraftTouched) return
      if (root.mode !== "manual") return
      root.manualDraft = root.service ? root.service.manualRpm : root.manualDraft
    }
    function onCurveChanged() {
      if (root.curveDraftTouched) return
      if (root.mode !== "curve") return
      if (root.service) root.curveDraft = root.service.curve
    }
  }

  function setMode(next) {
    if (!service) return
    if (next === "auto") {
      if (!canControl) return
      service.setControl({ mode: "auto" })
    } else if (next === "manual") {
      if (!canControl) return
      manualDraftTouched = false
      service.setControl({ mode: "manual", manual_rpm: manualDraft })
    } else if (next === "curve") {
      if (!canControl) return
      curveDraftTouched = false
      service.setControl({ mode: "curve", curve: curveDraft })
    }
  }

  function applyManual(rpm) {
    if (!canControl) return
    manualDraft = Math.round(Math.max(rpmMin, Math.min(rpmMax, rpm)))
    service.setControl({ mode: "manual", manual_rpm: manualDraft })
  }

  function curveRows() {
    var rows = []
    for (var i = 0; i < 5; i++) {
      var pair = curveDraft && curveDraft.length > i && Array.isArray(curveDraft[i]) ? curveDraft[i] : [45 + i * 12, 1400 + i * 950]
      var temp = Math.round(Number(pair && pair.length > 0 ? pair[0] : 45) || 45)
      var rpm = Math.round(Number(pair && pair.length > 1 ? pair[1] : 1400) || 1400)
      rows.push([Math.max(35, Math.min(92, temp)), Math.max(rpmMin, Math.min(rpmMax, rpm))])
    }
    return rows
  }

  function applyCurve() {
    if (!canControl) return
    var rows = curveRows()
    if (JSON.stringify(rows) === JSON.stringify(curveDraft)) {
      // nothing edited; still re-apply so the daemon re-ticks
    }
    service.setControl({ mode: "curve", curve: rows })
    curveDraftTouched = false
  }

  function setCurveRow(index, temp, rpm) {
    var rows = curveRows()
    rows[index] = [temp, rpm]
    curveDraft = rows
    curveDraftTouched = true
  }

  function curveRpmAt(temp) {
    var target = curveDraft.length > 0 ? curveDraft[0][1] : 1400
    for (var i = 0; i < curveDraft.length; i++) {
      if (temp >= Number(curveDraft[i][0])) target = Number(curveDraft[i][1])
    }
    return target
  }

  function curveSummary() {
    var parts = []
    for (var i = 0; i < curveDraft.length; i++) {
      if (Array.isArray(curveDraft[i])) {
        parts.push(Math.round(curveDraft[i][0]) + "°C " + Math.round(curveDraft[i][1]))
      }
    }
    return parts.length ? parts.join("  ·  ") : "—"
  }

  Component.onCompleted: { seedDrafts(); startFallback() }

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function toggle(): void { root.toggle() }
    function status(): string {
      return JSON.stringify({
        available: root.available, service: root.serviceUp,
        control_enabled: root.controlEnabled, mode: root.mode,
        fans: root.fans, manual_rpm: root.service ? root.service.manualRpm : 3000,
        curve: root.service ? root.service.curve : [],
        temperature: root.temperature, error: root.statusError, notice: root.notice })
    }
    function set(payload: string): string {
      if (!root.service) return "no-service"
      if (payload.length > 2048) return "rejected"
      try { return root.service.setControl(JSON.parse(payload)) ? "queued" : "rejected" }
      catch (e) { return "rejected" }
    }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.barLabel()
    dimmed: !root.serviceUp && !root.available
    active: root.mode !== "auto"
    tooltipText: root.barTooltip()
    onPressed: function(b) { root.toggle() }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(420))
    contentHeight: panel.fittedContentHeight(panelColumn.implicitHeight, Style.space(560))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        if (t === "a" || t === "A") root.setMode("auto")
        else if (t === "m" || t === "M") root.setMode("manual")
        else if (t === "c" || t === "C") root.setMode("curve")
      }

      ScrollView {
        id: scrollArea
        anchors.fill: parent
        clip: true

        Column {
          id: panelColumn
          width: scrollArea.availableWidth
          spacing: Style.space(14)

          Item {
            width: parent.width
            implicitHeight: Math.max(heroIcon.implicitHeight, heroLabels.implicitHeight)

            Text {
              id: heroIcon
              textFormat: Text.PlainText
              text: ""
              color: root.bar.foreground
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.display
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
            }

            Column {
              id: heroLabels
              anchors.left: heroIcon.right
              anchors.leftMargin: Style.space(14)
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(2)

              Text {
                text: "LegionFan"
                color: root.bar.foreground
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.title
                font.bold: true
                elide: Text.ElideRight
                width: parent.width
              }

              Text {
                textFormat: Text.PlainText
                text: {
                  if (!root.serviceUp) return "DAEMON UNAVAILABLE"
                  if (!root.available) return "NO FAN SENSORS"
                  return root.rpmText() + " · CPU " + root.tempText()
                }
                color: Qt.darker(root.bar.foreground, 1.4)
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
                font.letterSpacing: 1.2
                elide: Text.ElideRight
                width: parent.width
              }
            }
          }

          Text {
            visible: root.statusError !== ""
            width: parent.width
            textFormat: Text.PlainText
            text: root.statusError
            color: root.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          Text {
            visible: root.statusError === "" && root.notice !== ""
            width: parent.width
            textFormat: Text.PlainText
            text: root.notice
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          PanelSeparator { foreground: root.bar.foreground }

          // ---- Fan rows (one per hwmon fan channel) ----
          Column {
            visible: root.fans.length > 0
            width: parent.width
            spacing: Style.space(6)

            PanelSectionHeader {
              text: "FANS"
              foreground: root.bar.foreground
              fontFamily: root.fontFamily
            }

            Repeater {
              model: root.fans
              delegate: Row {
                width: panelColumn.width
                spacing: Style.space(10)
                Text {
                  textFormat: Text.PlainText
                  text: (modelData.name || "fan") + "   " + (typeof modelData.rpm === "number" ? modelData.rpm : 0) + " RPM"
                  color: root.bar.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                }
              }
            }
          }

          // ---- Mode selection ----
          Column {
            width: parent.width
            spacing: Style.space(10)

            PanelSectionHeader {
              text: "MODE"
              foreground: root.bar.foreground
              fontFamily: root.fontFamily
            }

            Row {
              width: parent.width
              spacing: Style.spacing.sm

              Button {
                text: "Auto"
                fontSize: Style.font.caption
                foreground: root.bar.foreground
                fontFamily: root.fontFamily
                bordered: true
                active: root.mode === "auto"
                enabled: root.canControl
                width: (parent.width - parent.spacing * 2) / 3
                onClicked: root.setMode("auto")
              }
              Button {
                text: "Manual"
                fontSize: Style.font.caption
                foreground: root.bar.foreground
                fontFamily: root.fontFamily
                bordered: true
                active: root.mode === "manual"
                enabled: root.canControl
                width: (parent.width - parent.spacing * 2) / 3
                onClicked: root.setMode("manual")
              }
              Button {
                text: "Curve"
                fontSize: Style.font.caption
                foreground: root.bar.foreground
                fontFamily: root.fontFamily
                bordered: true
                active: root.mode === "curve"
                enabled: root.canControl
                width: (parent.width - parent.spacing * 2) / 3
                onClicked: root.setMode("curve")
              }
            }
          }

          // ---- Manual RPM ----
          Column {
            visible: root.serviceUp
            width: parent.width
            spacing: Style.space(6)

            Item {
              width: parent.width
              implicitHeight: Math.max(manualHeader.implicitHeight, manualRpmText.implicitHeight)

              PanelSectionHeader {
                id: manualHeader
                text: "MANUAL TARGET RPM"
                foreground: root.bar.foreground
                fontFamily: root.fontFamily
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
              }

              Text {
                id: manualRpmText
                textFormat: Text.PlainText
                text: Math.round(manualSlider.dragging ? manualSlider.liveValue : root.manualDraft) + " RPM"
                color: Qt.darker(root.bar.foreground, 1.4)
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
                anchors.right: parent.right
                anchors.rightMargin: Style.space(6)
                anchors.verticalCenter: parent.verticalCenter
              }
            }

            PanelSlider {
              id: manualSlider
              bar: root.bar
              width: parent.width
              minimum: root.rpmMin
              maximum: root.rpmMax
              step: root.rpmStep
              integer: true
              value: root.manualDraft
              enabled: root.canControl
              opacity: root.canControl ? 1.0 : 0.45
              onMoved: function(v) {
                root.manualDraft = Math.round(v)
                root.manualDraftTouched = true
              }
              onReleased: function(v) { root.applyManual(v) }
            }
          }

          // ---- Temperature curve ----
          Column {
            visible: root.serviceUp
            width: parent.width
            spacing: Style.space(8)

            Item {
              width: parent.width
              implicitHeight: Math.max(curveHeader.implicitHeight, curveSummaryText.implicitHeight)

              PanelSectionHeader {
                id: curveHeader
                text: "TEMPERATURE CURVE"
                foreground: root.bar.foreground
                fontFamily: root.fontFamily
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
              }

              Text {
                id: curveSummaryText
                visible: root.canControl
                textFormat: Text.PlainText
                text: root.curveSummary()
                color: Qt.darker(root.bar.foreground, 1.4)
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
                width: Math.min(implicitWidth, parent.width * 0.55)
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
              }
            }

            Text {
              width: parent.width
              textFormat: Text.PlainText
              visible: root.temperature > 0 && root.canControl
              text: "At " + Math.round(root.temperature) + "°C this curve requests " +
                    root.curveRpmAt(root.temperature) + " RPM"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              wrapMode: Text.WordWrap
            }

            Repeater {
              model: 5
              delegate: CurveRow {
                width: panelColumn.width
                rowIndex: index
              }
            }

            Text {
              width: parent.width
              textFormat: Text.PlainText
              visible: root.canControl
              text: root.curveDraftTouched ? "Edited — press Apply curve" : "Fan RPM per temperature; edit and apply"
              color: root.curveDraftTouched ? root.urgent : root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              wrapMode: Text.WordWrap
            }

            Button {
              text: "Apply curve"
              fontSize: Style.font.caption
              foreground: root.bar.foreground
              fontFamily: root.fontFamily
              bordered: true
              enabled: root.canControl
              width: parent.width
              onClicked: root.applyCurve()
            }
          }
        }
      }
    }
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  component CurveRow: Item {
    id: curveRow
    property int rowIndex: 0
    readonly property var row: root.curveRows()[rowIndex]

    width: parent ? parent.width : 0
    implicitHeight: Math.max(tempLabel.implicitHeight, rpmSlider.implicitHeight)

    Text {
      id: tempLabel
      textFormat: Text.PlainText
      text: Math.round(curveRow.row[0]) + "°C"
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      width: Style.space(46)
    }

    PanelSlider {
      id: rpmSlider
      bar: root.bar
      anchors.left: parent.left
      anchors.leftMargin: Style.space(52)
      anchors.right: rpmText.left
      anchors.rightMargin: Style.space(10)
      anchors.verticalCenter: parent.verticalCenter
      minimum: root.rpmMin
      maximum: root.rpmMax
      step: root.rpmStep
      integer: true
      value: curveRow.row[1]
      enabled: root.canControl
      opacity: root.canControl ? 1.0 : 0.45
      onMoved: function(v) { root.setCurveRow(curveRow.rowIndex, curveRow.row[0], Math.round(v)) }
    }

    Text {
      id: rpmText
      textFormat: Text.PlainText
      text: (rpmSlider.dragging ? rpmSlider.liveValue : curveRow.row[1]) + ""
      color: root.bar.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      width: Style.space(64)
      horizontalAlignment: Text.AlignRight
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
    }
  }
}
