# SPDX-License-Identifier: MIT
"""Linux interfaces: sysfs and the WMI kernel module.

Nothing here decides policy; every function reads or writes one thing.
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path

SYS_VENDOR = Path("/sys/class/dmi/id/sys_vendor")
BOARD_NAME = Path("/sys/class/dmi/id/board_name")
PLATFORM_PROFILE = Path("/sys/firmware/acpi/platform_profile")
POWER_SUPPLIES = Path("/sys/class/power_supply")
LIDS = Path("/proc/acpi/button/lid")
HWMON = Path("/sys/class/hwmon")
CPUFREQ = Path("/sys/devices/system/cpu/cpufreq")
CPU_BOOST = CPUFREQ / "boost"
AMD_PSTATE_STATUS = Path("/sys/devices/system/cpu/amd_pstate/status")
WMI_PARAMETERS = Path("/sys/module/px13_tgp_probe/parameters")
CPU_SENSORS = ("k10temp", "zenpower", "coretemp")


class HardwareError(RuntimeError):
    pass


def read_text(path):
    try:
        return Path(path).read_text(encoding="ascii").strip()
    except OSError:
        return "unknown"


def read_int(path):
    try:
        return int(read_text(path))
    except ValueError:
        return None


def write_text(path, value, what):
    try:
        Path(path).write_text(f"{value}\n", encoding="ascii")
    except OSError as error:
        raise HardwareError(f"{what} write failed: {error}") from error


def run(command, timeout, env=None):
    """Runs a helper; returns (ok, stdout or error detail)."""
    try:
        result = subprocess.run(
            command, check=False, capture_output=True, text=True, timeout=timeout, env=env
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error)
    if result.returncode != 0:
        return False, result.stderr.strip() or result.stdout.strip()
    return True, result.stdout


def dmi_identity():
    return read_text(SYS_VENDOR), read_text(BOARD_NAME)


def sleep_system():
    """Hibernates when the system supports it, otherwise suspends; returns which."""
    ok, output = run(
        ["/usr/bin/busctl", "call", "org.freedesktop.login1", "/org/freedesktop/login1",
         "org.freedesktop.login1.Manager", "CanHibernate"],
        timeout=5,
    )
    action = "hibernate" if ok and '"yes"' in output else "suspend"
    ok, detail = run(["/usr/bin/systemctl", action], timeout=30)
    if not ok:
        raise HardwareError(f"{action} failed: {detail}")
    return action


def platform_profile(path=PLATFORM_PROFILE):
    """The mode chosen with Fn+F5, asusd or ROG Control Center, e.g. "balanced"."""
    return read_text(path)


@dataclass(frozen=True)
class BatteryReading:
    energy_wh: float | None
    voltage_v: float | None
    full: bool  # full or holding: no charge flow to measure
    critical: bool  # the gauge reads empty; energy_wh is not meaningful


class PowerSupplies:
    """Power sources, discovered by type rather than by name."""

    def __init__(self, root=POWER_SUPPLIES):
        self.root = Path(root)

    def _of_type(self, kind):
        return [path for path in sorted(self.root.glob("*")) if read_text(path / "type") == kind]

    def on_external_power(self):
        return any(read_text(path / "online") == "1" for path in self._of_type("Mains"))

    def usb_c(self):
        # UCSI supplies are online only for a USB-C PD source; a barrel
        # adapter shows up only as the mains supply.
        return any(read_text(path / "online") == "1" for path in self.root.glob("ucsi-source-psy-*"))

    def source(self):
        if not self.on_external_power():
            return "battery"
        return "usb-c" if self.usb_c() else "adapter"

    def _battery(self):
        # Peripheral batteries, such as a stylus, report scope "Device".
        batteries = [path for path in self._of_type("Battery") if read_text(path / "scope") != "Device"]
        return batteries[0] if batteries else None

    def battery(self):
        """The system battery's state, or None without one."""
        battery = self._battery()
        if battery is None:
            return None
        energy, voltage = read_int(battery / "energy_now"), read_int(battery / "voltage_now")
        return BatteryReading(
            energy_wh=None if energy is None else energy / 1_000_000,
            voltage_v=None if voltage is None else voltage / 1_000_000,
            full=read_text(battery / "status") in {"Full", "Not charging"},
            critical=read_text(battery / "capacity_level") == "Critical",
        )


def lid_open(root=LIDS):
    states = [read_text(path) for path in sorted(Path(root).glob("*/state"))]
    # A machine without a lid switch is treated as open.
    return not states or states[0].endswith("open")


class Cpu:
    def policies(self):
        return sorted(CPUFREQ.glob("policy*"))

    def ensure_active_pstate(self, log):
        # Guided and passive modes have no EPP, and their powersave governor
        # pins every core to the lowest frequency. Active mode lets the CPPC
        # firmware pick clocks within the PPT budget using the EPP hint.
        mode = read_text(AMD_PSTATE_STATUS)
        if mode in {"active", "unknown"}:
            return
        try:
            write_text(AMD_PSTATE_STATUS, "active", "amd-pstate mode")
        except HardwareError as error:
            log.warning("amd-pstate remains in %s mode (%s); using schedutil without EPP", mode, error)
            return
        log.info("amd-pstate switched from %s to active mode", mode)

    def set_policy(self, epp):
        """Removes any frequency cap and applies the energy preference."""
        policies = self.policies()
        if not policies:
            raise HardwareError("no CPU frequency policies available")
        # In active mode, powersave is the dynamic EPP-driven policy. In the
        # other modes powersave pins the CPU to its minimum, so a dynamic
        # governor is used instead.
        active = read_text(AMD_PSTATE_STATUS) == "active"
        governor = "powersave" if active else "schedutil"
        for policy in policies:
            maximum = read_int(policy / "cpuinfo_max_freq")
            if maximum is None:
                raise HardwareError(f"CPU hardware limit unreadable for {policy.name}")
            write_text(policy / "scaling_max_freq", maximum, f"{policy.name} maximum frequency")
            write_text(policy / "scaling_governor", governor, f"{policy.name} governor")
            epp_path = policy / "energy_performance_preference"
            if active and epp_path.exists():
                write_text(epp_path, epp, f"{policy.name} EPP")

    def set_boost(self, enabled):
        write_text(CPU_BOOST, 1 if enabled else 0, "CPU boost")

    def boost_active(self):
        if read_text(CPU_BOOST) != "1":
            return False
        policy = CPUFREQ / "policy0"
        current, hardware = read_int(policy / "scaling_max_freq"), read_int(policy / "cpuinfo_max_freq")
        # amd-pstate rounds the writable limit to the nearest supported
        # frequency (5157.000 vs 5157.895 MHz on the PX13).
        return current is not None and hardware is not None and current >= hardware - 2_000

    def temperature_c(self):
        for name in HWMON.glob("*/name"):
            if read_text(name) in CPU_SENSORS:
                millidegrees = read_int(name.parent / "temp1_input")
                return None if millidegrees is None else round(millidegrees / 1000, 1)
        return None

    def frequency_mhz(self):
        values = [read_int(policy / "scaling_cur_freq") for policy in self.policies()]
        values = [value for value in values if value is not None]
        return round(sum(values) / (1000 * len(values))) if values else None

    def info(self):
        policy = CPUFREQ / "policy0"
        return {
            "amd_pstate_mode": read_text(AMD_PSTATE_STATUS),
            "driver": read_text(policy / "scaling_driver"),
            "governor": read_text(policy / "scaling_governor"),
            "max_frequency_khz": read_text(policy / "scaling_max_freq"),
            "hardware_max_frequency_khz": read_text(policy / "cpuinfo_max_freq"),
            "epp": read_text(policy / "energy_performance_preference"),
        }


class Wmi:
    """Parameters of the px13_tgp_probe module, which performs the WMI calls."""

    NAMES = (
        "gpu_base_tgp",
        "gpu_tgp",
        "platform_mode",
        "gpu_dynamic_boost",
        "gpu_thermal_target",
        "cpu_spl",
        "cpu_sppt",
        "cpu_fppt",
    )
    CPU = ("cpu_spl", "cpu_sppt", "cpu_fppt")

    def __init__(self, root=WMI_PARAMETERS):
        self.root = Path(root)

    def read(self, name):
        return read_int(self.root / name)

    def write(self, name, value):
        write_text(self.root / name, value, f"WMI {name}={value}")

    def values(self):
        return {name: self.read(name) for name in self.NAMES}

    def base_tgp(self):
        return self.read("gpu_base_tgp")

    def has_cpu_power(self):
        return all((self.root / name).exists() for name in self.CPU)

    def cpu_power(self):
        return tuple(self.read(name) for name in self.CPU)

    def write_cpu_power(self, values):
        for name, value in zip(self.CPU, values, strict=True):
            self.write(name, value)

    def write_power_budget(self, profile):
        self.write("platform_mode", profile.platform_mode)
        self.write("gpu_tgp", profile.gpu_tgp_offset)
        self.write("gpu_dynamic_boost", profile.gpu_dynamic_boost)
        self.write("gpu_thermal_target", profile.gpu_thermal_target)
        self.write_cpu_power(profile.cpu)
