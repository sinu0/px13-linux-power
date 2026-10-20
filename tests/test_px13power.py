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
EXAMPLE = ROOT / "examples" / "profiles.d" / "120w.toml"
DMI = ("ASUSTeK COMPUTER INC.", "HN7306WV")
LOG = logging.getLogger("test")
LOG.addHandler(logging.NullHandler())
LOG.propagate = False


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{value}\n")


def load_model(*overlays):
    with tempfile.TemporaryDirectory() as overlay_dir:
        for overlay in overlays:
            shutil.copy(overlay, overlay_dir)
        return config.load(MODELS, overlay_dir, *DMI)


class Sysfs:
    """A writable fake of the power supply, lid, platform profile and WMI trees."""

    def __init__(self, root):
        self.root = Path(root)
        self.supplies = self.root / "power_supply"
        self.lids = self.root / "lid"
        self.wmi = self.root / "wmi"
        self.platform_profile = self.root / "platform_profile"
        write(self.supplies / "AC0" / "type", "Mains")
        write(self.supplies / "BAT0" / "type", "Battery")
        write(self.supplies / "BAT0" / "status", "Discharging")
        write(self.supplies / "BAT0" / "energy_now", 50_000_000)
        write(self.supplies / "BAT0" / "capacity_level", "Normal")
        write(self.supplies / "BAT0" / "voltage_now", 15_800_000)
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
        self.set_mode("balanced")

    def set_source(self, source):
        write(self.supplies / "AC0" / "online", int(source != "battery"))
        write(self.supplies / "ucsi-source-psy-USBC000:001" / "online", int(source == "usb-c"))

    def set_lid(self, is_open):
        write(self.lids / "LID" / "state", "state:      " + ("open" if is_open else "closed"))

    def set_mode(self, mode):
        write(self.platform_profile, mode)

    def set_critical(self, critical):
        write(self.supplies / "BAT0" / "capacity_level", "Critical" if critical else "Normal")
        if critical:
            self.set_energy(0)

    def set_voltage(self, volts):
        write(self.supplies / "BAT0" / "voltage_now", int(volts * 1_000_000))

    def set_energy(self, wh):
        write(self.supplies / "BAT0" / "energy_now", int(wh * 1_000_000))


class FakeNvidia:
    def __init__(self, maximum=120):
        self.maximum = maximum
        self.sleeping = False
        self.restarts = 0
        self.offsets = (0, 0)
        self.clock_limit = 0

    def suspended(self):
        return self.sleeping

    def describe(self):
        return {
            "default_power_w": 80.0,
            "max_power_w": float(self.maximum),
            "core_offset": [-1000, 1000],
            "memory_offset": [-2000, 6000],
            "clock_limit": [210, 3105, 15],
        }

    def ceiling_w(self):
        return None

    def restart_powerd(self):
        self.restarts += 1
        return True, ""

    def set_offsets(self, core, memory):
        self.offsets = (core, memory)

    def set_clock_limit(self, minimum, limit):
        self.clock_limit = limit

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
        self.model = load_model(*self.overlays)
        self.time = 1000.0
        self.sleep = lambda seconds: None
        self.hw = SimpleNamespace(
            supplies=hardware.PowerSupplies(self.sysfs.supplies),
            cpu=FakeCpu(),
            wmi=hardware.Wmi(self.sysfs.wmi),
            nvidia=FakeNvidia(self.vbios_max),
            lid_open=lambda: hardware.lid_open(self.sysfs.lids),
            platform_profile=lambda: hardware.platform_profile(self.sysfs.platform_profile),
        )
        self.controller = self.new_controller()

    def new_controller(self):
        controller = Controller(
            self.model,
            self.hw,
            LOG,
            state_file=self.tmp / "state.json",
            nvpcf_file=self.tmp / "nvpcf.conf",
            sleep=lambda seconds: self.sleep(seconds),
            now=lambda: self.time,
        )
        self.settle(controller)
        return controller

    def settle(self, controller=None):
        controller = controller or self.controller
        controller.evaluate()
        controller.wait_idle()

    def tick(self, seconds=1):
        self.time += seconds
        self.controller.guard_tick()
        self.controller.wait_idle()

    def mode(self, mode):
        self.sysfs.set_mode(mode)
        self.settle()
        return self.controller.profile


class ConfigTest(unittest.TestCase):
    def test_stock_model(self):
        model = load_model()
        self.assertEqual(list(model.profiles), ["Safe", "Quiet Gaming", "Gaming"])
        self.assertEqual(model.modes["performance"], ("Gaming",))
        self.assertEqual(model.tuning_requires, {"ac"})
        self.assertFalse(model.requires_cooling_confirmation)
        safe = model.profiles["Safe"]
        self.assertEqual(safe.for_source("battery").cpu_epp, "power")
        self.assertEqual(safe.for_source("adapter").cpu_epp, "balance_power")

    def test_example_overlay_adds_personal_policy(self):
        model = load_model(EXAMPLE)
        self.assertEqual(model.modes["performance"], ("120W", "Gaming"))
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
        self.assertIn("may be overridden", self.overlay_error("[gpu]\ntgp_offset = { min = 0, max = 90 }"))
        self.assertIn("unknown keys", self.overlay_error("[profiles.Gaming]\nturbo = true"))
        self.assertIn("default profile", self.overlay_error('[profiles.Safe]\nrequires = ["ac"]'))
        self.assertIn("unknown profiles", self.overlay_error('[modes]\nperformance = ["Turbo"]'))

    def test_unsupported_system(self):
        with self.assertRaisesRegex(config.ConfigError, "no model file"):
            config.load(MODELS, "/nonexistent", "ASUSTeK COMPUTER INC.", "GA403UV")

    def test_range_steps(self):
        limit = config.Range(210, 3105, 15)
        self.assertIn(1500, limit)
        self.assertNotIn(1505, limit)
        self.assertNotIn(3120, limit)


class HardwareTest(unittest.TestCase):
    def test_power_source_lid_and_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            sysfs = Sysfs(tmp)
            supplies = hardware.PowerSupplies(sysfs.supplies)
            self.assertEqual(supplies.source(), "adapter")
            sysfs.set_source("usb-c")
            self.assertEqual(supplies.source(), "usb-c")
            sysfs.set_source("battery")
            self.assertEqual(supplies.source(), "battery")
            battery = supplies.battery()
            self.assertEqual((battery.energy_wh, battery.voltage_v), (50.0, 15.8))  # not the stylus
            self.assertFalse(battery.full or battery.critical)
            sysfs.set_lid(False)
            self.assertFalse(hardware.lid_open(sysfs.lids))
            self.assertTrue(hardware.lid_open(Path(tmp) / "no-lid"))
            self.assertEqual(hardware.platform_profile(sysfs.platform_profile), "balanced")

    def test_wmi_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            sysfs = Sysfs(tmp)
            wmi = hardware.Wmi(sysfs.wmi)
            wmi.write_power_budget(load_model().profiles["Gaming"])
            self.assertEqual(wmi.cpu_power(), (30, 45, 55))
            self.assertEqual(wmi.read("gpu_tgp"), 25)
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


def reading(energy=50.0, voltage=15.8, full=False, critical=False):
    return hardware.BatteryReading(energy, voltage, full, critical)


class BalanceTest(unittest.TestCase):
    def setUp(self):
        self.balance = BatteryBalance(load_model().balance, 15, spl=15, now=0.0)

    def test_starts_at_the_profile_limit(self):
        self.assertEqual(self.balance.outputs(self.balance.level), (15, 0))

    def test_outputs_cut_cpu_before_gpu(self):
        self.assertEqual(self.balance.outputs(100), (25, 0))
        self.assertEqual(self.balance.outputs(50), (12, 0))
        self.assertEqual(self.balance.outputs(25), (12, 1800))
        self.assertEqual(self.balance.outputs(0), (12, 1200))

    def test_discharge_lowers_level(self):
        start, energy, targets = self.balance.level, 50.0, None
        for second in range(1, 31):
            energy -= 0.01  # 36 W discharge
            targets = self.balance.update(float(second), reading(energy)) or targets
        self.assertLess(self.balance.level, start)
        self.assertAlmostEqual(self.balance.net_power_w, -36.0, places=0)
        self.assertEqual(targets, self.balance.outputs(self.balance.level))

    def test_full_battery_holds_level(self):
        start = self.balance.level
        for second in range(1, 31):
            self.assertIsNone(self.balance.update(float(second), reading(full=True)))
        self.assertEqual(self.balance.level, start)

    def test_empty_gauge_uses_the_voltage_trend(self):
        start, targets = self.balance.level, None
        for second in range(1, 21):  # charging from a strong adapter
            targets = self.balance.update(float(second), reading(0.0, 13.0 + second * 0.002, critical=True)) or targets
        self.assertIsNone(targets)
        self.assertEqual(self.balance.level, start)
        self.assertFalse(self.balance.charger_too_weak)
        for second in range(21, 51):  # then discharging on a weak charger
            volts = 13.04 - (second - 20) * 0.003
            targets = self.balance.update(float(second), reading(0.0, volts, critical=True)) or targets
        self.assertEqual(targets, (12, 1200))
        self.assertTrue(self.balance.charger_too_weak)

    def test_clock_hysteresis(self):
        self.balance.gpu_clock = 1800
        self.assertFalse(self.balance.wants_clock(1815))
        self.assertTrue(self.balance.wants_clock(1845))
        self.assertTrue(self.balance.wants_clock(0))


class ModeTest(Fixture):
    def test_mode_selects_profile(self):
        self.assertEqual(self.controller.profile, "Safe")
        self.assertEqual(self.mode("quiet"), "Quiet Gaming")
        self.assertEqual(self.mode("performance"), "Gaming")
        self.assertEqual(self.hw.wmi.cpu_power(), (30, 45, 55))
        self.assertEqual(self.mode("low-power"), "Safe")  # a mode without profiles

    def test_gaming_follows_external_power(self):
        self.mode("performance")
        self.sysfs.set_source("battery")
        self.tick()
        self.assertEqual(self.controller.profile, "Safe")
        self.assertEqual(self.hw.cpu.epp, "power")
        self.sysfs.set_source("adapter")
        self.tick()
        self.assertEqual(self.controller.profile, "Gaming")

    def test_safe_battery_variant(self):
        restarts = self.hw.nvidia.restarts
        self.sysfs.set_source("battery")
        self.tick()
        self.assertEqual(self.hw.cpu.epp, "power")
        self.assertEqual(self.hw.nvidia.restarts, restarts)  # no restart on battery
        self.sysfs.set_source("adapter")
        self.tick()
        self.assertEqual(self.hw.cpu.epp, "balance_power")

    def test_requests_return_before_the_slow_work(self):
        self.mode("performance")
        restarts = self.hw.nvidia.restarts
        state = self.controller.set_gpu_power(90, 20, 84)
        self.assertTrue(state["applying"])
        self.assertEqual(state["gpu_power"], {"base_w": 90, "boost_w": 20, "temperature_c": 84})
        self.controller.set_gpu_power(95, 20, 84)
        self.controller.wait_idle()
        self.assertEqual(self.hw.nvidia.restarts, restarts + 1)  # two changes, one restart
        self.assertEqual(self.controller.nvpcf["max_tgp_w"], 115)
        self.assertFalse(self.controller.state()["applying"])

    def test_newer_work_supersedes_older_work(self):
        self.mode("performance")
        restarts = self.hw.nvidia.restarts
        self.controller.set_gpu_power(90, 20, 84)
        interrupted = []

        def sleep(seconds):
            if not interrupted:
                interrupted.append(True)
                self.controller.set_gpu_power(95, 25, 85)

        self.sleep = sleep
        self.controller.wait_idle()
        self.assertEqual(self.hw.nvidia.restarts, restarts + 1)
        self.assertEqual(self.controller.nvpcf["max_tgp_w"], 120)

    def test_interrupted_profile_change_is_completed(self):
        self.sysfs.set_mode("performance")
        self.controller.evaluate()  # Gaming is chosen, the work waits for asusd
        interrupted = []

        def sleep(seconds):
            if not interrupted:
                interrupted.append(True)
                self.controller.set_gpu_power(90, 20, 84)

        self.sleep = sleep
        self.controller.wait_idle()
        self.assertEqual(self.hw.cpu.epp, "performance")
        self.assertEqual(self.controller.nvpcf["max_tgp_w"], 110)

    def test_suspend_and_shutdown_hold_the_default(self):
        self.mode("performance")
        self.controller.prepare_suspend("test")
        self.assertEqual(self.controller.profile, "Safe")
        self.assertEqual(self.mode("quiet"), "Safe")  # held while asleep
        self.mode("performance")
        self.controller._resume_worker()
        self.controller.wait_idle()
        self.assertEqual(self.controller.profile, "Gaming")
        self.controller.shutdown()
        self.assertEqual(self.controller.profile, "Safe")
        self.assertEqual(self.hw.wmi.cpu_power(), (15, 20, 25))


class TuningTest(Fixture):
    def test_tuning_is_kept_per_profile(self):
        with self.assertRaisesRegex(RequestError, "not available in Safe"):
            self.controller.set_cpu_power(20, 30, 40)
        self.mode("performance")
        with self.assertRaisesRegex(RequestError, "sustained <= slow"):
            self.controller.set_cpu_power(40, 30, 40)
        state = self.controller.set_cpu_power(20, 30, 40)
        self.assertEqual(state["cpu_power"], {"sustained_w": 20, "slow_w": 30, "fast_w": 40})
        self.mode("balanced")
        self.mode("performance")
        self.assertEqual(self.hw.wmi.cpu_power(), (20, 30, 40))
        self.controller.reset()
        self.controller.wait_idle()
        self.assertEqual(self.hw.wmi.cpu_power(), (30, 45, 55))

    def test_gpu_power_validation(self):
        self.mode("performance")
        caps = self.controller.capabilities()["gpu_power"]
        self.assertEqual(caps["base"], {"min": 55, "max": 95, "step": 1})
        self.assertEqual(caps["max_total"], 120)
        with self.assertRaisesRegex(RequestError, "120 W in total"):
            self.controller.set_gpu_power(95, 26, 85)

    def test_overrides_survive_restart(self):
        self.mode("performance")
        self.controller.set_cpu_power(20, 30, 40)
        restarted = self.new_controller()
        self.assertEqual(restarted.profile, "Gaming")
        self.assertEqual(self.hw.wmi.cpu_power(), (20, 30, 40))

    def test_clocks(self):
        caps = self.controller.capabilities()["gpu_clocks"]
        self.assertEqual(caps["limit"], {"min": 210, "max": 3105, "step": 15})
        self.mode("performance")
        with self.assertRaisesRegex(RequestError, "15 MHz steps"):
            self.controller.set_gpu_clock_limit(1505)
        self.controller.set_gpu_offsets(100, 500)
        self.controller.set_gpu_clock_limit(1800)
        self.assertEqual((self.hw.nvidia.offsets, self.hw.nvidia.clock_limit), ((100, 500), 1800))
        self.mode("quiet")  # profiles never overclock
        self.assertEqual((self.hw.nvidia.offsets, self.hw.nvidia.clock_limit), ((0, 0), 0))
        with self.assertRaisesRegex(RequestError, "battery balance"):
            self.controller.set_gpu_clock_limit(1800)

    def test_clock_reset_waits_for_a_sleeping_gpu(self):
        self.mode("performance")
        self.controller.set_gpu_offsets(100, 0)
        self.hw.nvidia.sleeping = True
        self.mode("balanced")
        self.assertEqual(self.hw.nvidia.offsets, (100, 0))
        self.hw.nvidia.sleeping = False
        self.tick(60)
        self.assertEqual(self.hw.nvidia.offsets, (0, 0))

    def test_quiet_mode_saves_power_on_battery(self):
        self.sysfs.set_source("battery")
        self.assertEqual(self.mode("quiet"), "Quiet Gaming")
        self.assertEqual(self.hw.cpu.epp, "power")
        self.sysfs.set_source("usb-c")
        self.tick()
        self.assertEqual(self.hw.cpu.epp, "balance_performance")

    def empty_battery(self, volts_per_second):
        self.sysfs.set_critical(True)
        volts = 13.5
        for _ in range(25):
            volts += volts_per_second
            self.sysfs.set_voltage(volts)
            self.tick()

    def test_weak_charger_with_an_empty_battery(self):
        self.sysfs.set_source("usb-c")
        self.mode("quiet")
        self.empty_battery(-0.003)
        self.assertEqual(self.hw.wmi.cpu_power(), (12, 17, 22))
        self.assertEqual(self.hw.nvidia.clock_limit, 1200)
        self.assertIn("charger does not cover the load", self.controller.state()["last_error"])
        self.empty_battery(0.003)  # a stronger charger
        self.assertEqual(self.controller.state()["last_error"], "")

    def test_strong_adapter_with_an_empty_battery(self):
        self.mode("quiet")
        self.empty_battery(0.005)
        self.assertEqual(self.hw.wmi.cpu_power(), (15, 20, 25))
        self.assertEqual(self.controller.state()["last_error"], "")

    def test_quiet_gaming_balances_the_battery(self):
        self.mode("quiet")
        energy = 50.0
        for _ in range(60):  # 36 W discharge on a weak charger
            energy -= 0.01
            self.sysfs.set_energy(energy)
            self.tick()
        self.assertLess(self.hw.wmi.cpu_power()[0], 15)


class PersonalPolicyTest(Fixture):
    overlays = (EXAMPLE,)

    def test_adapter_and_lid(self):
        self.sysfs.set_source("usb-c")
        self.assertEqual(self.mode("performance"), "Safe")
        self.sysfs.set_source("adapter")
        self.tick()
        self.assertEqual(self.controller.profile, "Gaming")
        self.sysfs.set_lid(False)
        self.tick()
        self.assertEqual(self.controller.profile, "Safe")

    def test_cooling_confirmation_unlocks_120w(self):
        self.mode("performance")
        self.assertEqual(self.controller.profile, "Gaming")
        state = self.controller.state()
        self.assertEqual(state["blocked_reason"], "Changing limits requires cooling confirmation.")
        self.sysfs.set_lid(False)
        with self.assertRaisesRegex(RequestError, "lid open"):
            self.controller.confirm_cooling()
        self.sysfs.set_lid(True)
        self.tick()
        self.controller.confirm_cooling()
        self.controller.wait_idle()
        self.assertEqual(self.controller.profile, "120W")
        self.assertTrue(self.controller.state()["controls_ready"])
        self.assertEqual(self.controller.nvpcf["max_tgp_w"], 120)
        self.sysfs.set_lid(False)
        self.tick()
        self.sysfs.set_lid(True)
        self.tick()
        self.assertEqual(self.controller.profile, "Gaming")  # confirmation was cleared


class SmallVbiosTest(Fixture):
    overlays = (EXAMPLE,)
    vbios_max = 100

    def test_120w_is_skipped(self):
        self.mode("performance")
        self.controller.confirm_cooling()
        self.controller.wait_idle()
        self.assertEqual(self.controller.profile, "Gaming")


if __name__ == "__main__":
    unittest.main()
