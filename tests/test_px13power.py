# SPDX-License-Identifier: MIT
"""Tests of the controller and its parts on a simulated sysfs tree.

Run with: python3 -m unittest discover -s tests
"""

import logging
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "daemon"))

from px13power import config, hardware  # noqa: E402
from px13power.balance import BatteryBalance  # noqa: E402
from px13power.controller import Controller, RequestError  # noqa: E402
from px13power.nvidia import nvpcf_policy  # noqa: E402

MODELS = ROOT / "daemon" / "models"
DMI = ("ASUSTeK COMPUTER INC.", "HN7306WV")
LOG = logging.getLogger("test")
LOG.addHandler(logging.NullHandler())
LOG.propagate = False


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{value}\n")


class Sysfs:
    """A writable fake of the power supply, lid and WMI parameter trees."""

    def __init__(self, root):
        self.root = Path(root)
        self.supplies = self.root / "power_supply"
        self.lids = self.root / "lid"
        self.wmi = self.root / "wmi"
        write(self.supplies / "AC0" / "type", "Mains")
        write(self.supplies / "BAT0" / "type", "Battery")
        write(self.supplies / "BAT0" / "status", "Discharging")
        write(self.supplies / "BAT0" / "energy_now", 50_000_000)
        write(self.supplies / "ucsi-source-psy-USBC000:001" / "type", "USB")
        # A stylus battery sorts before the system battery.
        write(self.supplies / "A-stylus-battery" / "type", "Battery")
        write(self.supplies / "A-stylus-battery" / "scope", "Device")
        write(self.supplies / "A-stylus-battery" / "energy_now", 1_000)
        for name in hardware.Wmi.NAMES:
            write(self.wmi / name, 0)
        write(self.wmi / "gpu_base_tgp", 55)
        self.set_source("adapter")
        self.set_lid(True)

    def set_source(self, source):
        write(self.supplies / "AC0" / "online", int(source != "battery"))
        write(self.supplies / "ucsi-source-psy-USBC000:001" / "online", int(source == "usb-c"))

    def set_lid(self, is_open):
        write(self.lids / "LID" / "state", "state:      " + ("open" if is_open else "closed"))

    def wmi_value(self, name):
        return int((self.wmi / name).read_text())


class FakeNvidia:
    def __init__(self, maximum=120):
        self.limits = (80.0, float(maximum))
        self.clock_limit = 0
        self.offsets = (0, 0)

    def suspended(self):
        return False

    def power_limits(self):
        return self.limits

    def ceiling_limit(self):
        return None

    def restart_powerd(self, action):
        return True, ""

    def set_offsets(self, core, memory):
        self.offsets = (core, memory)
        return True

    def set_clock_limit(self, limit):
        self.clock_limit = limit
        return True

    def telemetry(self):
        return {}

    def driver_bound(self):
        return True

    def responds(self):
        return True


class FakeCpu:
    def __init__(self):
        self.epp = None

    def ensure_active_pstate(self, log):
        pass

    def set_policy(self, epp):
        self.epp = epp

    def set_boost(self, enabled):
        pass

    def boost_active(self):
        return True

    def temperature_c(self):
        return None

    def frequency_mhz(self):
        return None

    def info(self):
        return {}


class Fixture(unittest.TestCase):
    overlays = ()
    vbios_max = 120

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.sysfs = Sysfs(self.tmp / "sys")
        overlay_dir = self.tmp / "profiles.d"
        overlay_dir.mkdir()
        for overlay in self.overlays:
            shutil.copy(ROOT / overlay, overlay_dir)
        self.model = config.load(MODELS, overlay_dir, *DMI)
        self.asus_profiles = []
        self.time = 1000.0
        self.hw = SimpleNamespace(
            supplies=hardware.PowerSupplies(self.sysfs.supplies),
            cpu=FakeCpu(),
            wmi=hardware.Wmi(self.sysfs.wmi),
            nvidia=FakeNvidia(self.vbios_max),
            lid_open=lambda: hardware.lid_open(self.sysfs.lids),
            set_asus_profile=self.asus_profiles.append,
        )
        self.controller = self.new_controller()

    def new_controller(self):
        controller = Controller(
            self.model,
            self.hw,
            LOG,
            state_file=self.tmp / "state.json",
            nvpcf_file=self.tmp / "nvpcf.conf",
            sleep=lambda seconds: None,
            now=lambda: self.time,
        )
        with controller.lock:
            controller._fallback("test start")
            controller._restore_requested()
        return controller

    def tick(self, seconds=1):
        self.time += seconds
        self.controller.guard_tick()


class ConfigTest(unittest.TestCase):
    def test_stock_model(self):
        model = config.load(MODELS, "/nonexistent", *DMI)
        self.assertEqual(list(model.profiles), ["Safe", "Quiet Gaming", "Gaming"])
        self.assertEqual(model.tuning_requires, {"ac"})
        self.assertFalse(model.requires_cooling_confirmation)
        safe = model.profiles["Safe"]
        self.assertEqual(safe.for_source("battery").asus_profile, "Quiet")
        self.assertEqual(safe.for_source("adapter").asus_profile, "Balanced")

    def test_example_overlay_adds_personal_policy(self):
        with tempfile.TemporaryDirectory() as overlay_dir:
            shutil.copy(ROOT / "examples/profiles.d/120w.toml", overlay_dir)
            model = config.load(MODELS, overlay_dir, *DMI)
        self.assertEqual(list(model.profiles), ["Safe", "Quiet Gaming", "Gaming", "120W"])
        self.assertEqual(model.profiles["Gaming"].requires, {"adapter", "lid_open"})
        self.assertEqual(model.profiles["Gaming"].gpu_tgp_offset, 25)
        self.assertTrue(model.requires_cooling_confirmation)

    def overlay_error(self, text):
        with tempfile.TemporaryDirectory() as overlay_dir:
            (Path(overlay_dir) / "bad.toml").write_text(text)
            with self.assertRaises(config.ConfigError) as context:
                config.load(MODELS, overlay_dir, *DMI)
        return str(context.exception)

    def test_rejects_invalid_overlays(self):
        self.assertIn("unknown requirements", self.overlay_error('[tuning]\nrequires = ["moon"]'))
        self.assertIn("cpu must fit", self.overlay_error("[profiles.Gaming]\ncpu = [60, 45, 55]"))
        self.assertIn("outside", self.overlay_error("[profiles.Gaming]\ngpu_dynamic_boost = 40"))
        self.assertIn("only [tuning]", self.overlay_error('[gpu]\ndefault_tgp_w = 200'))
        self.assertIn("unknown keys", self.overlay_error("[profiles.Gaming]\nturbo = true"))
        self.assertIn("default profile", self.overlay_error('[profiles.Safe]\nrequires = ["ac"]'))

    def test_unsupported_system(self):
        with self.assertRaisesRegex(config.ConfigError, "no model file"):
            config.load(MODELS, "/nonexistent", "ASUSTeK COMPUTER INC.", "GA403UV")

    def test_range_steps(self):
        limit = config.Range(210, 3105, 15)
        self.assertIn(1500, limit)
        self.assertNotIn(1505, limit)
        self.assertNotIn(3120, limit)


class HardwareTest(unittest.TestCase):
    def test_power_source_and_lid(self):
        with tempfile.TemporaryDirectory() as tmp:
            sysfs = Sysfs(tmp)
            supplies = hardware.PowerSupplies(sysfs.supplies)
            self.assertEqual(supplies.source(), "adapter")
            sysfs.set_source("usb-c")
            self.assertEqual(supplies.source(), "usb-c")
            sysfs.set_source("battery")
            self.assertEqual(supplies.source(), "battery")
            self.assertEqual(supplies.battery_energy_wh(), 50.0)
            self.assertFalse(supplies.battery_full())
            sysfs.set_lid(False)
            self.assertFalse(hardware.lid_open(sysfs.lids))
            self.assertTrue(hardware.lid_open(Path(tmp) / "no-lid"))

    def test_wmi_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            sysfs = Sysfs(tmp)
            wmi = hardware.Wmi(sysfs.wmi)
            model = config.load(MODELS, "/nonexistent", *DMI)
            wmi.write_power_budget(model.profiles["Gaming"])
            self.assertEqual(wmi.cpu_power(), (30, 45, 55))
            self.assertEqual(sysfs.wmi_value("gpu_tgp"), 25)
            self.assertEqual(wmi.base_tgp(), 55)


class NvpcfTest(unittest.TestCase):
    profile = SimpleNamespace(gpu_tgp_offset=40, gpu_dynamic_boost=25)

    def test_raises_base_above_default(self):
        policy = nvpcf_policy(80, 120, 55, self.profile)
        self.assertEqual((policy["ctgp_offset_w"], policy["dynamic_boost_w"], policy["max_tgp_w"]), (15, 25, 120))

    def test_trims_boost_to_vbios_maximum(self):
        policy = nvpcf_policy(80, 100, 55, self.profile)
        self.assertEqual((policy["ctgp_offset_w"], policy["dynamic_boost_w"], policy["max_tgp_w"]), (15, 5, 100))

    def test_never_goes_below_default(self):
        policy = nvpcf_policy(80, 120, 55, SimpleNamespace(gpu_tgp_offset=0, gpu_dynamic_boost=5))
        self.assertEqual((policy["ctgp_offset_w"], policy["base_tgp_w"]), (0, 80))


class BalanceTest(unittest.TestCase):
    def setUp(self):
        model = config.load(MODELS, "/nonexistent", *DMI)
        self.balance = BatteryBalance(model.balance, 15, spl=15, now=0.0)

    def test_outputs_cut_cpu_before_gpu(self):
        self.assertEqual(self.balance.outputs(100), (25, 0))
        self.assertEqual(self.balance.outputs(75), (18, 0))
        self.assertEqual(self.balance.outputs(50), (12, 0))
        self.assertEqual(self.balance.outputs(25), (12, 1800))
        self.assertEqual(self.balance.outputs(0), (12, 1200))

    def test_discharge_lowers_level(self):
        energy = 50.0
        targets = None
        for second in range(1, 31):
            energy -= 0.01  # 36 W discharge
            targets = self.balance.update(float(second), energy, battery_full=False) or targets
        self.assertLess(self.balance.level, 70.0)
        self.assertAlmostEqual(self.balance.net_power_w, -36.0, places=0)
        self.assertEqual(targets, self.balance.outputs(self.balance.level))

    def test_full_battery_holds_level(self):
        for second in range(1, 31):
            self.assertIsNone(self.balance.update(float(second), 50.0, battery_full=True))
        self.assertEqual(self.balance.level, 70.0)

    def test_clock_hysteresis(self):
        self.balance.gpu_clock = 1800
        self.assertFalse(self.balance.wants_clock(1815))
        self.assertTrue(self.balance.wants_clock(1845))
        self.assertTrue(self.balance.wants_clock(0))


class StockControllerTest(Fixture):
    def test_starts_in_default_profile(self):
        state = self.controller.state()
        self.assertEqual(state["active_profile"], "Safe")
        self.assertFalse(state["controls_ready"])
        self.assertIn("other than Safe", state["blocked_reason"])
        self.assertEqual(self.asus_profiles[-1], "Balanced")
        self.assertEqual(state["gpu_power"], {"base_w": 65, "boost_w": 5, "temperature_c": 80})

    def test_gaming_requires_external_power(self):
        self.sysfs.set_source("battery")
        with self.assertRaisesRegex(RequestError, "requires external power"):
            self.controller.activate("Gaming")

    def test_gaming_falls_back_when_unplugged(self):
        self.controller.activate("Gaming")
        self.assertTrue(self.controller.state()["controls_ready"])
        self.sysfs.set_lid(False)  # the stock policy allows a closed lid
        self.tick()
        self.assertEqual(self.controller.active_profile, "Gaming")
        self.sysfs.set_source("battery")
        self.tick()
        self.assertEqual(self.controller.active_profile, "Safe")

    def test_safe_follows_power_source_after_debounce(self):
        self.sysfs.set_source("battery")
        for _ in range(3):
            self.tick()
        self.assertEqual(self.asus_profiles[-1], "Balanced")
        self.tick()
        self.assertEqual(self.asus_profiles[-1], "Quiet")
        self.assertEqual(self.hw.cpu.epp, "power")

    def test_cpu_tuning_is_kept_per_profile(self):
        with self.assertRaisesRegex(RequestError, "other than Safe"):
            self.controller.set_cpu_power(20, 30, 40)
        self.controller.activate("Gaming")
        with self.assertRaisesRegex(RequestError, "sustained <= slow"):
            self.controller.set_cpu_power(40, 30, 40)
        state = self.controller.set_cpu_power(20, 30, 40)
        self.assertEqual(state["cpu_power"], {"sustained_w": 20, "slow_w": 30, "fast_w": 40})
        self.controller.activate("Safe")
        self.controller.activate("Gaming")
        self.assertEqual(self.hw.wmi.cpu_power(), (20, 30, 40))
        self.controller.reset()
        self.assertEqual(self.hw.wmi.cpu_power(), (30, 45, 55))

    def test_gpu_power_validation(self):
        self.controller.activate("Gaming")
        caps = self.controller.capabilities()["gpu_power"]
        self.assertEqual(caps["base"], {"min": 55, "max": 95, "step": 1})
        self.assertEqual(caps["max_total"], 120)
        with self.assertRaisesRegex(RequestError, "120 W in total"):
            self.controller.set_gpu_power(95, 26, 85)
        state = self.controller.set_gpu_power(90, 20, 84)
        self.assertEqual(state["gpu_power"], {"base_w": 90, "boost_w": 20, "temperature_c": 84})
        self.assertEqual(self.controller.nvpcf["max_tgp_w"], 110)

    def test_overrides_survive_restart(self):
        self.controller.activate("Gaming")
        self.controller.set_cpu_power(20, 30, 40)
        restarted = self.new_controller()
        self.assertEqual(restarted.requested_profile, "Gaming")
        self.assertEqual(restarted.active_profile, "Safe")  # Gaming has requirements
        self.assertEqual(restarted.overrides["Gaming"]["cpu"], [20, 30, 40])

    def test_quiet_gaming_is_restored_and_balanced(self):
        self.controller.activate("Quiet Gaming")
        restarted = self.new_controller()
        self.assertEqual(restarted.active_profile, "Quiet Gaming")
        self.controller = restarted
        self.assertFalse(restarted.state()["clocks_enabled"])
        # A 36 W discharge on a weak charger lowers the level by 3 % a period.
        energy = 50.0
        for _ in range(60):
            energy -= 0.01
            write(self.sysfs.supplies / "BAT0" / "energy_now", int(energy * 1_000_000))
            self.tick()
        self.assertLess(self.hw.wmi.cpu_power()[0], 15)
        self.assertLess(restarted.balance.level, 60)

    def test_capabilities_follow_the_vbios(self):
        caps = self.controller.capabilities()
        self.assertEqual(caps["profiles"], ["Safe", "Quiet Gaming", "Gaming"])
        self.assertFalse(caps["requires_cooling_confirmation"])


class PersonalPolicyTest(Fixture):
    overlays = ("examples/profiles.d/120w.toml",)

    def test_lid_and_adapter_are_enforced(self):
        self.sysfs.set_source("usb-c")
        with self.assertRaisesRegex(RequestError, "power adapter"):
            self.controller.activate("Gaming")
        self.sysfs.set_source("adapter")
        self.controller.activate("Gaming")
        self.sysfs.set_lid(False)
        self.tick()
        self.assertEqual(self.controller.active_profile, "Safe")

    def test_120w_needs_cooling_confirmation(self):
        with self.assertRaisesRegex(RequestError, "cooling confirmation"):
            self.controller.activate("120W")
        self.sysfs.set_lid(False)
        with self.assertRaisesRegex(RequestError, "lid open"):
            self.controller.confirm_cooling()
        self.sysfs.set_lid(True)
        self.controller.confirm_cooling()
        state = self.controller.activate("120W")
        self.assertEqual(state["active_profile"], "120W")
        self.assertTrue(state["controls_ready"])
        self.assertEqual(self.controller.nvpcf["max_tgp_w"], 120)

    def test_tuning_requires_all_conditions(self):
        self.controller.activate("Gaming")
        state = self.controller.state()
        self.assertEqual(state["blocked_reason"], "Changing limits requires cooling confirmation.")
        with self.assertRaises(RequestError):
            self.controller.set_gpu_offsets(100, 0)


class SmallVbiosTest(Fixture):
    overlays = ("examples/profiles.d/120w.toml",)
    vbios_max = 100

    def test_120w_is_hidden(self):
        self.assertEqual(self.controller.capabilities()["profiles"], ["Safe", "Quiet Gaming", "Gaming"])


if __name__ == "__main__":
    unittest.main()
