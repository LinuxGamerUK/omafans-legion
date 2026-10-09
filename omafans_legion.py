#!/usr/bin/env python3
"""LegionFan controller daemon (stdlib only).

Reads hwmon fan sensors (lenovo_wmi_other) + thermal zones + GPUs, and writes
fan*_target RPM setpoints. Exposes a bounded Unix-socket protocol to the QML
widget. Design inherited from OmaFans' security model, adapted to Legion
hardware which has no kernel watchdog.
"""
import argparse
import glob
import json
import math
import os
import select
import signal
import socket
import stat
import struct
import sys
import time
from pathlib import Path

SOCKET_PATH = "/run/omafans-legion/control.sock"
CONFIG_PATH = "/etc/omafans-legion/controller.json"
HWMON_NAME = "lenovo_wmi_other"
MAX_MESSAGE = 4096
IO_TIMEOUT = 0.25
CLIENT_TIMEOUT = 2.0
HEARTBEAT_TIMEOUT = 15.0
POLL_SECONDS = 1.5
SENSOR_RECOVERY_SAMPLES = 3
DEFAULT_CURVE = [[45, 1400], [60, 2400], [72, 3400], [82, 4400], [92, 5200]]
CURVE_MIN_RPM = 1200     # below this the EC may not sustain control
CURVE_MAX_RPM = 5300     # measured ceiling for this chassis
MIN_TEMP_C = 35          # curve temps start here


def default_state():
    return {"mode": "auto", "manual_rpm": 3000,
            "curve": [list(row) for row in DEFAULT_CURVE]}


def valid_curve(curve):
    # 5 points, temps strictly rising in [MIN_TEMP_C, 92], rpm nondecreasing.
    if not isinstance(curve, list) or len(curve) != 5:
        return False
    previous_temp = MIN_TEMP_C - 10
    previous_rpm = 0
    for point in curve:
        if not isinstance(point, list) or len(point) != 2:
            return False
        temp, rpm = point
        if type(temp) is not int or type(rpm) is not int:
            return False
        if not previous_temp < temp <= 92:
            return False
        if not previous_rpm <= rpm <= CURVE_MAX_RPM:
            return False
        previous_temp, previous_rpm = temp, rpm
    return previous_rpm >= 4000  # must end at a high-speed point


def validate_update(payload, previous):
    if not isinstance(payload, dict) or not payload:
        raise ValueError("Expected a nonempty settings object")
    if set(payload) - {"mode", "manual_rpm", "curve"}:
        raise ValueError("Unknown setting")
    state = dict(previous)
    if "mode" in payload:
        if payload["mode"] not in ("auto", "manual", "curve"):
            raise ValueError("Invalid mode")
        state["mode"] = payload["mode"]
    if "manual_rpm" in payload:
        value = payload["manual_rpm"]
        if type(value) is not int or not CURVE_MIN_RPM <= value <= CURVE_MAX_RPM:
            raise ValueError(f"Manual RPM must be an integer {CURVE_MIN_RPM}-{CURVE_MAX_RPM}")
        state["manual_rpm"] = value
    if "curve" in payload:
        if not valid_curve(payload["curve"]):
            raise ValueError("Curve needs five rising temps (35-92C), nondecreasing RPM, ending >= 4000")
        state["curve"] = [list(row) for row in payload["curve"]]
    return state


def decode_message(raw):
    if len(raw) > MAX_MESSAGE:
        raise ValueError("Message too large")
    def reject_constant(value):
        raise ValueError("Non-finite JSON number")
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    try:
        value = json.loads(raw, parse_constant=reject_constant,
                           object_pairs_hook=unique_object)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("Invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 8:
            raise ValueError("JSON nesting is too deep")
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("Non-finite JSON number")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return value


def encode_message(value):
    raw = json.dumps(value, allow_nan=False, separators=(",", ":")).encode() + b"\n"
    if len(raw) > MAX_MESSAGE:
        raise ValueError("Message too large")
    return raw


def receive_message(connection, timeout):
    deadline = time.monotonic() + timeout
    raw = bytearray()
    while b"\n" not in raw:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Request deadline exceeded")
        connection.settimeout(remaining)
        chunk = connection.recv(min(1024, MAX_MESSAGE + 1 - len(raw)))
        if not chunk:
            raise ValueError("Incomplete message")
        raw.extend(chunk)
        if len(raw) > MAX_MESSAGE:
            raise ValueError("Message too large")
    line, tail = raw.split(b"\n", 1)
    if tail.strip():
        raise ValueError("One request per connection")
    return decode_message(line)


def read_number(path):
    try:
        with open(path, encoding="ascii") as stream:
            return int(stream.read(64).strip())
    except (OSError, ValueError, UnicodeError):
        return None


class Hardware:
    """Fan sensors from lenovo_wmi_other (or any hwmon exposing fan%d_input).

    Writes are restricted to fan*_target files inside that single hwmon node.
    """

    def __init__(self, hwmon_name=HWMON_NAME):
        self.root = None
        self.name = hwmon_name
        for candidate in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            try:
                node = Path(candidate)
                if node.joinpath("name").read_text(encoding="ascii").strip() == hwmon_name:
                    self.root = node.resolve(strict=True)
                    break
            except (OSError, UnicodeError):
                continue

    @property
    def available(self):
        return self.root is not None

    def fans(self):
        result = []
        if not self.root:
            return result
        for path in sorted(self.root.glob("fan*_input")):
            name = path.name[:-len("_input")]
            if not name[3:].isdigit():
                continue
            rpm = read_number(path)
            if rpm is None:
                continue
            result.append({"name": name, "rpm": rpm})
        return result

    def _fan_files(self):
        return sorted(self.root.glob("fan*_target")) if self.root else []

    def write_target(self, name, rpm):
        """rpm==0 releases the fan back to firmware control."""
        if not self.root:
            return False
        target = self.root / f"{name}_target"
        try:
            if target.resolve(strict=True).parent != self.root:
                return False
        except OSError:
            return False
        try:
            with open(target, "w", encoding="ascii") as stream:
                stream.write(str(rpm))
            return True
        except OSError:
            return False

    def read_target(self, name):
        if not self.root:
            return None
        return read_number(self.root / f"{name}_target")

    def has_control(self):
        return bool(self.root and self._fan_files())


class Thermals:
    """CPU package + GPU + first sensor set. Read-only, never fails."""

    CPU_ZONES = ("x86_pkg_temp", "TCPU")
    GPU_CPU_IDENT = ("coretemp",)

    def __init__(self):
        pass

    @staticmethod
    def _zones(types):
        out = []
        for path in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
            try:
                ztype = Path(path, "type").read_text(encoding="ascii").strip()
            except (OSError, UnicodeError):
                continue
            if ztype in types:
                value = read_number(Path(path, "temp"))
                if value is not None and 0 <= value <= 125000:
                    out.append((ztype, value / 1000))
        return out

    @classmethod
    def cpu_temp(cls):
        for _t, value in cls._zones(cls.CPU_ZONES):
            return round(value, 1)
        return None

    @classmethod
    def gpu_temp(cls):
        for _t, value in cls._zones(("iwlwifi_1",)):
            return None  # wifi sensor is not the dGPU; N/A by design
        return None

    @classmethod
    def hottest_zone(cls):
        best = None
        for path in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
            try:
                ztype = Path(path, "type").read_text(encoding="ascii").strip()
            except (OSError, UnicodeError):
                continue
            if ztype in ("x86_pkg_temp", "iwlwifi_1"):
                continue  # package/wifi already counted elsewhere
            value = read_number(Path(path, "temp"))
            if value is None or not 0 <= value <= 125000:
                continue
            celsius = value / 1000
            if best is None or celsius > best[1]:
                best = (ztype, round(celsius, 1))
        return best


class Controller:
    """Owns the control loop. Fail-closed: any write/read-back fault returns
    both fans to firmware control (target 0) and latches a visible fault.
    There is no kernel watchdog on this hardware, so the daemon must never
    leave a stale manual RPM behind."""

    def __init__(self, hardware):
        self.hardware = hardware
        self.state = default_state()
        self.last_heartbeat = None
        self.error = ""
        self.notice = ""
        self.fault = False
        self.write_failures = 0

    # ---- safety ---------------------------------------------------------
    def safe(self):
        """Release both fans to firmware control. Verified via read-back."""
        released = True
        for fan in self.hardware.fans():
            name = fan["name"]
            if not self.hardware.write_target(name, 0):
                released = False
                continue
            if self.hardware.read_target(name) != 0:
                released = False
        self.state["mode"] = "auto"
        if released:
            self.notice = ""
        else:
            self.notice = "Could not return fans to firmware control; check the daemon"
        return released

    def fail(self, message):
        self.fault = True
        self.error = message
        rescued = self.safe()
        if not rescued:
            self.error += "; release to firmware control failed"
        print("LegionFan: " + self.error, file=sys.stderr, flush=True)
        return False

    # ---- control --------------------------------------------------------
    def target_for(self, temperature):
        """Stepped curve lookup: the highest threshold <= temperature wins."""
        mode = self.state["mode"]
        if mode == "manual":
            return max(CURVE_MIN_RPM, min(CURVE_MAX_RPM, self.state["manual_rpm"]))
        target = self.state["curve"][0][1]
        for threshold, rpm in self.state["curve"]:
            if temperature is not None and temperature >= threshold:
                target = rpm
        return max(CURVE_MIN_RPM, min(CURVE_MAX_RPM, target))

    def tick(self):
        if self.fault:
            return False
        hw = self.hardware
        if not hw.has_control():
            self.error = ""
            self.notice = "Legion fan interface unavailable; monitoring only"
            return False
        temperature = Thermals.cpu_temp()
        if temperature is None:
            return self.fail("CPU temperature sensor unavailable")
        if temperature >= 95:
            # Software safeguard. Release to firmware - the EC's own thermal
            # table is better informed than one sysfs input.
            self.error = f"Very high CPU temperature ({temperature}C): released fans to firmware control"
            if not self.safe():
                return self.fail(self.error + "; release failed")
            self.fault = True
            return False
        age = None if self.last_heartbeat is None else time.monotonic() - self.last_heartbeat
        if age is None or age > HEARTBEAT_TIMEOUT or self.state["mode"] == "auto":
            self.error = ""
            if not self.safe():
                return self.fail("Unable to release fans to firmware control")
            return True
        target = self.target_for(temperature)
        written = True
        for fan in self.hardware.fans():
            name = fan["name"]
            if not self.hardware.write_target(name, target):
                written = False
                continue
            readback = self.hardware.read_target(name)
            if readback != target:
                written = False
        if not written:
            self.write_failures += 1
            if self.write_failures >= 3:
                return self.fail("Fan RPM write failed")
            return False
        self.write_failures = 0
        self.error = ""
        self.notice = ""
        return True

    # ---- reporting ------------------------------------------------------
    def status(self):
        hw = self.hardware
        fans = hw.fans()
        temperature = Thermals.cpu_temp()
        hottest = Thermals.hottest_zone()
        control = bool(fans) and not self.fault
        return {**self.state,
                "available": hw.available,
                "service": True,
                "control_enabled": control,
                "fans": fans,
                "temperature": temperature,
                "hottest": {"zone": hottest[0], "celsius": hottest[1]} if hottest else None,
                "error": self.error,
                "notice": self.notice}

    def request(self, request):
        command = request.get("command")
        if command not in ("status", "heartbeat", "set"):
            raise ValueError("Unknown command")
        allowed = {"command", "settings"} if command == "set" else {"command"}
        if set(request) - allowed:
            raise ValueError("Unknown request field")
        if command == "set":
            state = validate_update(request.get("settings"), self.state)
            if self.fault:
                return {**self.status(), "ok": False}
            self.state = state
        if command in ("heartbeat", "set"):
            if command == "heartbeat" and self.last_heartbeat is not None:
                if time.monotonic() - self.last_heartbeat > HEARTBEAT_TIMEOUT:
                    self.state["mode"] = "auto"
            self.last_heartbeat = time.monotonic()
        good = self.tick() if command == "set" else not self.fault
        return {**self.status(), "ok": good}


def read_config(path=CONFIG_PATH):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077 or info.st_nlink != 1:
            raise ValueError("Controller configuration must be a private root-owned regular file")
        config = decode_message(os.read(fd, MAX_MESSAGE + 1))
    finally:
        os.close(fd)
    if set(config) != {"uid", "gid"} or any(type(v) is not int or v <= 0 for v in config.values()):
        raise ValueError("Invalid controller UID/GID")
    return config


def handle_connection(connection, controller, allowed_uid):
    with connection:
        try:
            credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _, uid, _ = struct.unpack("3i", credentials)
            if uid != allowed_uid:
                return
            request = receive_message(connection, IO_TIMEOUT)
            response = controller.request(request)
        except (ValueError, OSError, TimeoutError):
            response = {"ok": False, "error": "Invalid or incomplete request"}
        try:
            connection.settimeout(IO_TIMEOUT)
            connection.sendall(encode_message(response))
        except OSError:
            return


def serve():
    if os.geteuid() != 0:
        raise ValueError("The daemon must be started by its system service")
    config = read_config()
    hw = Hardware()
    if not hw.available:
        raise ValueError("lenovo_wmi_other hwmon node not found; check module parameters")
    runtime = Path(SOCKET_PATH).parent
    info = runtime.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError("Unsafe runtime directory")
    controller = Controller(hw)
    running = True
    def stop(signum, frame):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        Path(SOCKET_PATH).unlink(missing_ok=True)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(SOCKET_PATH)
            # Root-owned parent prevents socket replacement; SO_PEERCRED
            # authenticates independently of the inode mode.
            os.chmod(SOCKET_PATH, 0o600)
            os.chown(SOCKET_PATH, config["uid"], config["gid"])
            listener.listen(4)
            next_tick = time.monotonic()
            while running:
                now = time.monotonic()
                if now >= next_tick:
                    controller.tick()
                    next_tick = time.monotonic() + POLL_SECONDS
                readable, _, _ = select.select([listener], [], [], min(0.25, max(0, next_tick - time.monotonic())))
                if readable:
                    connection, _ = listener.accept()
                    handle_connection(connection, controller, config["uid"])
    finally:
        controller.safe()  # ALWAYS release firmware control on exit
        Path(SOCKET_PATH).unlink(missing_ok=True)
    return 0


def offline_status():
    """Unprivileged monitoring fallback: sensors are world-readable, control is not."""
    hw = Hardware()
    fans = hw.fans()
    temperature = Thermals.cpu_temp()
    hottest = Thermals.hottest_zone()
    return {"available": hw.available, "service": False, "control_enabled": False,
            "mode": "auto", "manual_rpm": 3000,
            "curve": [list(row) for row in DEFAULT_CURVE],
            "fans": fans, "temperature": temperature,
            "hottest": {"zone": hottest[0], "celsius": hottest[1]} if hottest else None,
            "error": "", "notice": "LegionFan daemon unavailable; monitoring only"}


def client_request(request, path=SOCKET_PATH):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(CLIENT_TIMEOUT)
        connection.connect(path)
        credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _, uid, _ = struct.unpack("3i", credentials)
        if uid != 0:
            raise ValueError("Daemon is not root-owned")
        connection.sendall(encode_message(request))
        return receive_message(connection, CLIENT_TIMEOUT)


def client_main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    command = args[0] if args else "status"
    try:
        if command not in ("status", "heartbeat", "set") or len(args) > (2 if command == "set" else 1):
            raise ValueError("Use status, heartbeat, or set JSON")
        request = {"command": command}
        if command == "set":
            payload = decode_message((args[1] if len(args) == 2 else "{}").encode())
            validate_update(payload, default_state())
            request["settings"] = payload
        try:
            response = client_request(request)
        except (OSError, ValueError):
            response = offline_status()
            response["ok"] = command != "set"
            if command == "set":
                response["error"] = "Control unavailable; settings were not applied"
        print(encode_message(response).decode(), end="")
        return 0 if response.get("ok", True) else 1
    except (ValueError, OSError):
        print('{"ok":false,"error":"Invalid request"}')
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("serve", "release"))
    args = parser.parse_args()
    try:
        if args.command == "release":
            hw = Hardware()
            if not hw.available:
                return 0
            return 0 if Controller(hw).safe() else 1
        return serve()
    except (ValueError, OSError) as exc:
        print(f"LegionFan: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
