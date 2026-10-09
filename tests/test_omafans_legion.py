"""Offline unit tests for LegionFan. Never touch real hardware."""
import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "omafans_legion", Path(__file__).parents[1] / "omafans_legion.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# Stub the live thermal reads: host temperature must not decide a test.
_FAKE_TEMP = {"c": 65.0, "zone": "TCPU"}
mod.Thermals.cpu_temp = staticmethod(lambda: _FAKE_TEMP["c"])
mod.Thermals.hottest_zone = staticmethod(lambda: (_FAKE_TEMP["zone"], _FAKE_TEMP["c"]))


class FakeHardware:
    def __init__(self, fans=(("fan1", 2800), ("fan2", 2800)), control=True, succeeds=True):
        self._fans = list(fans)
        self._control = control
        self._succeeds = succeeds
        self.targets = {}
        self.write_count = 0

    @property
    def available(self):
        return bool(self._fans)

    def fans(self):
        return [{"name": n, "rpm": r} for n, r in self._fans]

    def has_control(self):
        return self._control

    def write_target(self, name, rpm):
        self.write_count += 1
        if not self._succeeds:
            return False
        self.targets[name] = rpm
        return True

    def read_target(self, name):
        return self.targets.get(name)


class CurveTests(unittest.TestCase):
    def test_default_curve_valid(self):
        self.assertTrue(mod.valid_curve(mod.DEFAULT_CURVE))

    def test_curve_needs_five_rising_points(self):
        self.assertFalse(mod.valid_curve([[40, 100], [50, 200], [60, 300]]))
        self.assertFalse(mod.valid_curve([[60, 100], [50, 200], [40, 300], [30, 400], [20, 500]]))
        self.assertFalse(mod.valid_curve([[40, 100], [50, 200], [60, 300], [70, 400], [80, 300]]))

    def test_curve_bounds(self):
        self.assertFalse(mod.valid_curve([[30, 100], [40, 200], [50, 300], [60, 400], [70, 500]]))
        self.assertFalse(mod.valid_curve([[45, 100], [55, 200], [65, 300], [75, 400], [95, 500]]))
        self.assertFalse(mod.valid_curve([[45, 100], [55, 200], [65, 300], [75, 9000], [85, 5000]]))

    def test_curve_rejects_bad_types(self):
        self.assertFalse(mod.valid_curve([[45, 1400.5], [55, 2400], [65, 3400], [75, 4400], [85, 5200]]))
        self.assertFalse(mod.valid_curve("not a list"))


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.state = mod.default_state()

    def test_manual_rpm_bounds(self):
        with self.assertRaises(ValueError):
            mod.validate_update({"manual_rpm": 500}, self.state)
        with self.assertRaises(ValueError):
            mod.validate_update({"manual_rpm": 9999}, self.state)
        state = mod.validate_update({"manual_rpm": 3200}, self.state)
        self.assertEqual(state["manual_rpm"], 3200)

    def test_mode_enum(self):
        with self.assertRaises(ValueError):
            mod.validate_update({"mode": "turbo"}, self.state)
        self.assertEqual(mod.validate_update({"mode": "curve"}, self.state)["mode"], "curve")

    def test_unknown_field_rejected(self):
        with self.assertRaises(ValueError):
            mod.validate_update({"fanctl": "rm -rf /"}, self.state)


class ControllerTests(unittest.TestCase):
    def test_auto_releases_to_zero_target(self):
        hw = FakeHardware()
        c = mod.Controller(hw)
        c.state["mode"] = "auto"
        c.last_heartbeat = __import__("time").monotonic()
        self.assertTrue(c.tick())
        self.assertEqual(hw.targets, {"fan1": 0, "fan2": 0})

    def test_manual_sets_rpm_target_on_all_fans(self):
        hw = FakeHardware()
        c = mod.Controller(hw)
        c.state["mode"] = "manual"
        c.state["manual_rpm"] = 3000
        c.last_heartbeat = __import__("time").monotonic()
        self.assertTrue(c.tick())
        self.assertEqual(hw.targets, {"fan1": 3000, "fan2": 3000})

    def test_curve_uses_temp_step(self):
        hw = FakeHardware()
        c = mod.Controller(hw)
        c.state["mode"] = "curve"
        c.state["curve"] = [[45, 1400], [60, 2400], [72, 3400], [82, 4400], [92, 5200]]
        c.last_heartbeat = __import__("time").monotonic()
        c.tick()
        # current CPU temp is whatever the host reports; just assert sane targets
        self.assertTrue(hw.targets)
        for _name, rpm in hw.targets.items():
            self.assertIn(rpm, (1400, 2400, 3400, 4400, 5200))

    def test_write_failure_latches_fault(self):
        hw = FakeHardware(succeeds=False)
        c = mod.Controller(hw)
        c.state["mode"] = "manual"
        c.state["manual_rpm"] = 3000
        c.last_heartbeat = __import__("time").monotonic()
        # manual RPM target cannot be written nor released; three consecutive
        # failed ticks latch the fault
        c.tick(); c.tick(); c.tick()
        self.assertTrue(c.fault)
        self.assertIn("release", c.error.lower())
        # faulted controller must not keep writing
        writes_after_fault = hw.write_count
        c.tick()
        self.assertEqual(hw.write_count, writes_after_fault)

    def test_manual_write_readback_mismatch_counts_strikes(self):
        # Hardware that accepts writes but reads back the wrong value: safe()
        # succeeds (0 reads back 0), manual RPM write is "rejected", so the
        # three-strike latch governs.
        class MismatchHardware(FakeHardware):
            def read_target(self, name):
                return self.targets.get(name, 0) if name not in self.targets else 4242
        hw = MismatchHardware()
        # pre-seed so first manual write mismatches
        hw.targets["fan1"] = 9999
        hw.targets["fan2"] = 9999
        c = mod.Controller(hw)
        c.state["mode"] = "manual"
        c.state["manual_rpm"] = 3000
        c.last_heartbeat = __import__("time").monotonic()
        c.tick()
        self.assertFalse(c.fault)   # first strike only
        c.tick()
        c.tick()                    # third consecutive
        self.assertTrue(c.fault)
        self.assertTrue(c.error.startswith("Fan RPM write failed"))

    def test_safe_returns_fans_to_firmware(self):
        hw = FakeHardware()
        c = mod.Controller(hw)
        c.state["mode"] = "manual"
        c.state["manual_rpm"] = 4000
        c.last_heartbeat = __import__("time").monotonic()
        c.tick()
        c.safe()
        self.assertEqual(hw.targets["fan1"], 0)
        self.assertEqual(hw.targets["fan2"], 0)


class MessageTests(unittest.TestCase):
    def test_rejects_non_object(self):
        with self.assertRaises(ValueError):
            mod.decode_message(b"[1,2,3]")

    def test_rejects_duplicate_keys(self):
        with self.assertRaises(ValueError):
            mod.decode_message(b'{"mode":"auto","mode":"curve"}')

    def test_rejects_non_finite(self):
        with self.assertRaises(ValueError):
            mod.decode_message(b'{"a": NaN}')

    def test_rejects_deep_nesting(self):
        with self.assertRaises(ValueError):
            mod.decode_message(b"[" * 20 + b"]" * 20)

    def test_accepts_valid(self):
        value = mod.decode_message(b'{"command":"status"}')
        self.assertEqual(value, {"command": "status"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
