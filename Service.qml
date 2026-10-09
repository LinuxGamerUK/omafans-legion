import QtQuick
import Quickshell.Io

Item {
  id: root
  property var shell: null
  property var manifest: null
  property string omarchyPath: ""
  property bool available: false
  property bool serviceUp: false
  property bool controlEnabled: false
  property string mode: "auto"
  property int manualRpm: 3000
  property var curve: [[45, 1400], [60, 2400], [72, 3400], [82, 4400], [92, 5200]]
  property var fans: []
  property real temperature: 0
  property string hottestZone: ""
  property real hottestCelsius: 0
  property string error: ""
  property string notice: ""
  readonly property bool busy: helper.running
  readonly property string helperPath: decodeURIComponent(Qt.resolvedUrl("fanctl.py").toString().replace(/^file:\/\//, ""))

  function unavailable(message) {
    available = false
    serviceUp = false
    controlEnabled = false
    error = message
  }

  function refresh() {
    if (helper.running) return
    helper.command = ["/usr/bin/python3", helperPath, "heartbeat"]
    helper.running = true
  }

  function setControl(payload) {
    if (helper.running) return false
    var data = JSON.stringify(payload)
    if (!data || data.length > 2048) return false
    helper.command = ["/usr/bin/python3", helperPath, "set", data]
    helper.running = true
    return true
  }

  function applyStatus(raw, exitCode) {
    var parsed = null
    try { parsed = JSON.parse(String(raw)) } catch (e) { parsed = null }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      unavailable("LegionFan helper returned invalid status")
      return
    }
    available = parsed.available === true
    serviceUp = parsed.service === true
    controlEnabled = parsed.control_enabled === true
    mode = ["auto", "manual", "curve"].indexOf(parsed.mode) >= 0 ? parsed.mode : "auto"
    manualRpm = typeof parsed.manual_rpm === "number" && isFinite(parsed.manual_rpm)
               ? parsed.manual_rpm : 3000
    curve = Array.isArray(parsed.curve) && parsed.curve.length === 5 ? parsed.curve : curve
    fans = Array.isArray(parsed.fans) ? parsed.fans : []
    temperature = typeof parsed.temperature === "number" && isFinite(parsed.temperature) ? parsed.temperature : 0
    if (parsed.hottest && typeof parsed.hottest === "object") {
      hottestZone = typeof parsed.hottest.zone === "string" ? parsed.hottest.zone : ""
      hottestCelsius = typeof parsed.hottest.celsius === "number" && isFinite(parsed.hottest.celsius)
                       ? parsed.hottest.celsius : 0
    }
    error = typeof parsed.error === "string" ? parsed.error.slice(0, 250) : ""
    if (exitCode !== 0 && error === "") error = "LegionFan request failed"
    notice = typeof parsed.notice === "string" ? parsed.notice.slice(0, 250) : ""
  }

  Timer {
    interval: 3000
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  Process {
    id: helper
    stdout: StdioCollector { id: output; waitForEnd: true }
    onExited: function(exitCode) { root.applyStatus(output.text, exitCode) }
  }

  IpcHandler {
    target: "community.omafanslegion.service"
    function status(): string {
      return JSON.stringify({ available: root.available, service: root.serviceUp,
        control_enabled: root.controlEnabled, mode: root.mode, fans: root.fans,
        manual_rpm: root.manualRpm, curve: root.curve,
        temperature: root.temperature, hottest_zone: root.hottestZone,
        hottest_celsius: root.hottestCelsius,
        error: root.error, notice: root.notice })
    }
    function refresh(): void { root.refresh() }
    function set(payload: string): string {
      if (payload.length > 2048) return "rejected"
      try { return root.setControl(JSON.parse(payload)) ? "queued" : "rejected" }
      catch (e) { return "rejected" }
    }
  }
}
