#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Single owner of PX13 performance controls.

The NVIDIA mobile power limit is programmed through ASUS WMI, not NVML.  This
daemon serializes all WMI writes and restores a safe state after suspend.
"""

import json
import logging
import subprocess
import threading
import time
from pathlib import Path

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib


BUS_NAME = "org.asuslinux.Px13Power"
OBJECT_PATH = "/org/asuslinux/Px13Power"
INTERFACE = "org.asuslinux.Px13Power"
MODULE_PARAMETERS = Path("/sys/module/px13_tgp_probe/parameters")
CPU_BOOST = Path("/sys/devices/system/cpu/cpufreq/boost")
AC_ONLINE = Path("/sys/class/power_supply/AC0/online")
LID_STATE = Path("/proc/acpi/button/lid/LID/state")
HWMON_PATH = Path("/sys/class/hwmon")
CPU_POLICIES = Path("/sys/devices/system/cpu/cpufreq")
AMD_PSTATE_STATUS = Path("/sys/devices/system/cpu/amd_pstate/status")
STATE_FILE = Path("/var/lib/px13-powerd/state.json")
NVPCF_OVERRIDE_FILE = Path("/run/px13-powerd/nvpcf-override.conf")
NVIDIA_DEFAULT_TGP_FALLBACK_W = 80
SYS_VENDOR = Path("/sys/class/dmi/id/sys_vendor")
BOARD_NAME = Path("/sys/class/dmi/id/board_name")
SUPPORTED_SYS_VENDOR = "ASUSTeK COMPUTER INC."
SUPPORTED_BOARD_NAME = "HN7306WV"

# These are the ranges advertised by this RTX 4060's NVIDIA controls.  They
# are capability bounds, not stability recommendations.
GPU_CORE_OFFSET_MIN_MHZ = -1000
GPU_CORE_OFFSET_MAX_MHZ = 1000
GPU_MEMORY_OFFSET_MIN_MHZ = -2000
GPU_MEMORY_OFFSET_MAX_MHZ = 6000
GPU_CLOCK_LIMIT_MIN_MHZ = 210
GPU_CLOCK_LIMIT_MAX_MHZ = 3105
GPU_CLOCK_LIMIT_STEP_MHZ = 15
CPU_SPL_MIN_W = 5
CPU_SPL_MAX_W = 45
CPU_SPPT_MIN_W = 5
CPU_SPPT_MAX_W = 55
CPU_FPPT_MIN_W = 5
CPU_FPPT_MAX_W = 65

PROFILES = {
    "Safe": {
        "gpu_tgp_offset": 10,
        "platform_mode": 0,
        "gpu_dynamic_boost": 5,
        "gpu_thermal_target": 80,
        "gpu_offsets": (0, 0),
        "gpu_clock_limit_mhz": 0,
        # Explicit low PPT values avoid retaining the previous Gaming/120W
        # budget while the Safe profile has CPU boost disabled.
        "cpu": (15, 20, 25),
        # Keep CPB enabled in the driver. On amd-pstate guided mode, toggling
        # global boost down to 0 prevents a later profile from raising its
        # scaling_max_freq above the nominal clock. The 2 GHz policy cap is
        # the effective Safe limit.
        "boost": True,
        "cpu_max_frequency_khz": 2_000_000,
        "cpu_epp": "balance_power",
        "cpu_governor": "powersave",
        "asus_profile": "Balanced",
    },
    "Gaming": {
        "gpu_tgp_offset": 25,
        "platform_mode": 1,
        "gpu_dynamic_boost": 15,
        "gpu_thermal_target": 85,
        "gpu_offsets": (0, 0),
        "gpu_clock_limit_mhz": 0,
        "cpu": (30, 45, 55),
        "boost": True,
        "cpu_max_frequency_khz": None,
        "cpu_epp": "performance",
        "cpu_governor": "schedutil",
        "asus_profile": "Performance",
    },
    "120W": {
        "gpu_tgp_offset": 40,
        # Keep the firmware in Performance so its fan curve remains active.
        # GPU VAR, Dynamic Boost and PPT are independent WMI controls.
        "platform_mode": 1,
        "gpu_dynamic_boost": 25,
        "gpu_thermal_target": 87,
        # Public profiles never overclock implicitly. Offsets are opt-in.
        "gpu_offsets": (0, 0),
        # Zero resets the NVIDIA clock lock, preserving normal GPU Boost.
        "gpu_clock_limit_mhz": 0,
        # A 5 W sustained budget preserves the full 25 W Dynamic Boost, while
        # higher slow/fast limits avoid the permanent 600 MHz behavior seen at
        # 5/5/5 W. Raising SPL is available through the UI but trades GPU watts
        # for CPU performance on this shared platform budget.
        "cpu": (5, 25, 35),
        "boost": True,
        "cpu_max_frequency_khz": None,
        "cpu_epp": "performance",
        "cpu_governor": "schedutil",
        "asus_profile": "Performance",
    },
}


class Px13PowerError(RuntimeError):
    pass


class Px13Power(dbus.service.Object):
    def __init__(self, bus):
        self.log = logging.getLogger("px13-powerd")
        self.lock = threading.RLock()
        self._verify_platform()
        self.bus_name = dbus.service.BusName(BUS_NAME, bus=bus)
        super().__init__(self.bus_name, OBJECT_PATH)
        self.active_profile = "Safe"
        self.requested_profile = "Safe"
        self.gpu_core_offset_mhz = 0
        self.gpu_memory_offset_mhz = 0
        self.gpu_clock_limit_mhz = 0
        self.cpu_overrides = {}
        self.nvpcf_override = {}
        self.cooling_confirmed = False
        self.last_error = ""
        self.resuming = False
        self.resume_worker_started = False
        self._telemetry_cache_time = 0.0
        self._telemetry_cache = {}
        self._load_state()
        self._write_nvpcf_override(PROFILES["Safe"])
        self._subscribe_sleep(bus)
        self._start_guard()

    def _read_text(self, path):
        try:
            return path.read_text(encoding="ascii").strip()
        except OSError:
            return "unknown"

    def _verify_platform(self):
        vendor = self._read_text(SYS_VENDOR)
        board = self._read_text(BOARD_NAME)
        if vendor != SUPPORTED_SYS_VENDOR or board != SUPPORTED_BOARD_NAME:
            raise Px13PowerError(
                f"unsupported system: vendor={vendor!r}, board={board!r}; "
                f"expected {SUPPORTED_SYS_VENDOR!r}/{SUPPORTED_BOARD_NAME!r}"
            )

    def _on_ac(self):
        return self._read_text(AC_ONLINE) == "1"

    def _lid_open(self):
        return self._read_text(LID_STATE).endswith("open")

    def _boost_active(self):
        if self._read_text(CPU_BOOST) != "1":
            return False
        policy = CPU_POLICIES / "policy0"
        try:
            current_limit = int((policy / "scaling_max_freq").read_text(encoding="ascii"))
            hardware_limit = int((policy / "cpuinfo_max_freq").read_text(encoding="ascii"))
        except (OSError, ValueError):
            return False
        # amd-pstate rounds the writable policy limit to the nearest supported
        # frequency (5157.000 vs 5157.895 MHz on this CPU).
        return current_limit >= hardware_limit - 2_000

    def _wmi_values(self):
        return {
            name: self._read_text(MODULE_PARAMETERS / name)
            for name in (
                "gpu_base_tgp",
                "gpu_tgp",
                "platform_mode",
                "gpu_dynamic_boost",
                "gpu_thermal_target",
                "cpu_spl",
                "cpu_sppt",
                "cpu_fppt",
            )
        }

    def _cpu_policy(self):
        policy = CPU_POLICIES / "policy0"
        return {
            "amd_pstate_mode": self._read_text(AMD_PSTATE_STATUS),
            "driver": self._read_text(policy / "scaling_driver"),
            "governor": self._read_text(policy / "scaling_governor"),
            "max_frequency_khz": self._read_text(policy / "scaling_max_freq"),
            "hardware_max_frequency_khz": self._read_text(policy / "cpuinfo_max_freq"),
            "epp": self._read_text(policy / "energy_performance_preference"),
        }

    def _set_cpu_frequency_policy(self, max_frequency_khz, epp, governor):
        policies = sorted(CPU_POLICIES.glob("policy*"))
        if not policies:
            raise Px13PowerError("no CPU frequency policies available")
        pstate_mode = self._read_text(AMD_PSTATE_STATUS)
        for policy in policies:
            value = max_frequency_khz
            if value is None:
                try:
                    value = int((policy / "cpuinfo_max_freq").read_text(encoding="ascii"))
                except (OSError, ValueError) as error:
                    raise Px13PowerError(f"CPU hardware limit read failed: {error}") from error
            try:
                (policy / "scaling_max_freq").write_text(f"{value}\n", encoding="ascii")
                (policy / "scaling_governor").write_text(f"{governor}\n", encoding="ascii")
                epp_path = policy / "energy_performance_preference"
                if pstate_mode == "active" and epp_path.exists():
                    epp_path.write_text(f"{epp}\n", encoding="ascii")
            except OSError as error:
                raise Px13PowerError(f"CPU policy write failed for {policy.name}: {error}") from error

    def _cpu_temperature(self):
        for name_path in HWMON_PATH.glob("*/name"):
            if self._read_text(name_path) != "k10temp":
                continue
            try:
                millidegrees = int((name_path.parent / "temp1_input").read_text(encoding="ascii"))
            except (OSError, ValueError):
                return None
            return round(millidegrees / 1000.0, 1)
        return None

    def _cpu_frequency(self):
        values = []
        for policy in CPU_POLICIES.glob("policy*"):
            try:
                values.append(int((policy / "scaling_cur_freq").read_text(encoding="ascii")))
            except (OSError, ValueError):
                continue
        if not values:
            return None
        return round(sum(values) / (1000.0 * len(values)))

    @staticmethod
    def _telemetry_number(value):
        value = value.strip()
        if value in {"", "N/A", "[N/A]"}:
            return None
        try:
            return float(value)
        except ValueError:
            return None

    def _telemetry(self):
        now = time.monotonic()
        if now - self._telemetry_cache_time < 1.0:
            return self._telemetry_cache

        telemetry = {
            "gpu_power_w": None,
            "gpu_temperature_c": None,
            "gpu_clock_mhz": None,
            "cpu_temperature_c": self._cpu_temperature(),
            "cpu_frequency_mhz": self._cpu_frequency(),
        }
        try:
            result = subprocess.run(
                [
                    "/usr/bin/nvidia-smi",
                    "--query-gpu=power.draw,temperature.gpu,clocks.current.graphics",
                    "--format=csv,noheader,nounits",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            result = None

        if result and result.returncode == 0 and result.stdout.strip():
            fields = [self._telemetry_number(value) for value in result.stdout.splitlines()[0].split(",")]
            if len(fields) == 3:
                telemetry["gpu_power_w"] = fields[0]
                telemetry["gpu_temperature_c"] = fields[1]
                telemetry["gpu_clock_mhz"] = fields[2]

        self._telemetry_cache_time = now
        self._telemetry_cache = telemetry
        return telemetry

    def _state(self):
        return {
            "active_profile": self.active_profile,
            "requested_profile": self.requested_profile,
            "ac_online": self._on_ac(),
            "lid_open": self._lid_open(),
            "cooling_confirmed": self.cooling_confirmed,
            "cpu_boost": self._boost_active(),
            "resuming": self.resuming,
            "last_error": self.last_error,
            "gpu_offsets": {
                "core_mhz": self.gpu_core_offset_mhz,
                "memory_mhz": self.gpu_memory_offset_mhz,
            },
            "gpu_clock_limit_mhz": self.gpu_clock_limit_mhz,
            "nvpcf_override": self.nvpcf_override,
            "wmi": self._wmi_values(),
            "cpu_policy": self._cpu_policy(),
            "telemetry": self._telemetry(),
        }

    def _state_json(self):
        return json.dumps(self._state(), sort_keys=True)

    def _capabilities_json(self):
        try:
            base_tgp = int(self._read_text(MODULE_PARAMETERS / "gpu_base_tgp"))
        except ValueError:
            base_tgp = 0
        hardware_max = self._hardware_max_tgp()
        power_available = (
            base_tgp > 0 and hardware_max is not None and hardware_max >= base_tgp + 5
        )
        maximum_base = (
            min(base_tgp + 40, hardware_max - 5) if power_available else base_tgp
        )
        profiles = [
            name
            for name in PROFILES
            if name == "Safe" or self._profile_supported(name, hardware_max)
        ]
        return json.dumps(
            {
                "provider": "NVIDIA GPU tuning",
                "profiles": profiles,
                "requires_cooling_confirmation": True,
                "power": {
                    "available": power_available,
                    "base_power": {
                        "min": base_tgp,
                        "max": maximum_base,
                        "step": 1,
                    },
                    "dynamic_boost": {"min": 5, "max": 25, "step": 1},
                    "temperature_target": {"min": 75, "max": 87, "step": 1},
                    "max_total": hardware_max or 0,
                },
                "cpu_power": {
                    "available": all(
                        (MODULE_PARAMETERS / name).exists()
                        for name in ("cpu_spl", "cpu_sppt", "cpu_fppt")
                    ),
                    "sustained": {"min": CPU_SPL_MIN_W, "max": CPU_SPL_MAX_W, "step": 1},
                    "slow": {"min": CPU_SPPT_MIN_W, "max": CPU_SPPT_MAX_W, "step": 1},
                    "fast": {"min": CPU_FPPT_MIN_W, "max": CPU_FPPT_MAX_W, "step": 1},
                },
                "clocks": {
                    "core_offset": {
                        "min": GPU_CORE_OFFSET_MIN_MHZ,
                        "max": GPU_CORE_OFFSET_MAX_MHZ,
                        "step": 1,
                    },
                    "memory_offset": {
                        "min": GPU_MEMORY_OFFSET_MIN_MHZ,
                        "max": GPU_MEMORY_OFFSET_MAX_MHZ,
                        "step": 1,
                    },
                    "clock_limit": {
                        "min": GPU_CLOCK_LIMIT_MIN_MHZ,
                        "max": GPU_CLOCK_LIMIT_MAX_MHZ,
                        "step": GPU_CLOCK_LIMIT_STEP_MHZ,
                        "default": 0,
                    },
                },
            },
            sort_keys=True,
        )

    def _persist_state(self):
        STATE_FILE.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        temporary = STATE_FILE.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "requested_profile": self.requested_profile,
                    "cpu_overrides": self.cpu_overrides,
                },
                sort_keys=True,
            ),
            encoding="ascii",
        )
        temporary.replace(STATE_FILE)

    def _load_state(self):
        try:
            data = json.loads(STATE_FILE.read_text(encoding="ascii"))
            requested = data.get("requested_profile", "Safe")
            if requested in PROFILES:
                self.requested_profile = requested
            overrides = data.get("cpu_overrides", {})
            if isinstance(overrides, dict):
                for profile, values in overrides.items():
                    if profile in PROFILES and profile != "Safe" and self._valid_cpu_power(values):
                        self.cpu_overrides[profile] = [int(value) for value in values]
        except (OSError, ValueError, TypeError):
            pass

    def _write_parameter(self, name, value):
        path = MODULE_PARAMETERS / name
        try:
            path.write_text(f"{value}\n", encoding="ascii")
        except OSError as error:
            raise Px13PowerError(f"WMI write {name}={value} failed: {error}") from error

    def _set_boost(self, enabled):
        try:
            CPU_BOOST.write_text("1\n" if enabled else "0\n", encoding="ascii")
        except OSError as error:
            raise Px13PowerError(f"CPU boost write failed: {error}") from error

    def _set_asus_profile(self, profile):
        result = subprocess.run(
            ["/usr/bin/asusctl", "profile", "set", profile],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise Px13PowerError(f"ASUS profile {profile} failed: {detail}")

    def _gpu_power_limits(self):
        try:
            result = subprocess.run(
                [
                    "/usr/bin/nvidia-smi",
                    "--query-gpu=power.default_limit,power.max_limit",
                    "--format=csv,noheader,nounits",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return (None, None)
        if result.returncode != 0 or not result.stdout.strip():
            return (None, None)
        try:
            default_limit, max_limit = result.stdout.splitlines()[0].split(",")
            return (float(default_limit.strip()), float(max_limit.strip()))
        except (ValueError, TypeError):
            return (None, None)

    def _hardware_max_tgp(self):
        _, max_limit = self._gpu_power_limits()
        if max_limit is None or max_limit <= 0:
            return None
        return int(max_limit + 0.001)

    def _profile_requested_total(self, name):
        try:
            base_tgp = int(self._read_text(MODULE_PARAMETERS / "gpu_base_tgp"))
        except ValueError:
            return None
        profile = PROFILES[name]
        return base_tgp + profile["gpu_tgp_offset"] + profile["gpu_dynamic_boost"]

    def _profile_supported(self, name, hardware_max=None):
        if name == "Safe":
            return True
        if hardware_max is None:
            hardware_max = self._hardware_max_tgp()
        requested_total = self._profile_requested_total(name)
        return (
            hardware_max is not None
            and requested_total is not None
            and requested_total <= hardware_max
        )

    def _gpu_ceiling_limit(self):
        try:
            result = subprocess.run(
                ["/usr/bin/nvidia-smi", "-q", "-d", "POWER"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        in_ceiling_section = False
        for line in result.stdout.splitlines():
            if "GPU Ceiling Power Limit" in line:
                in_ceiling_section = True
                continue
            if in_ceiling_section and "Current Power Limit" in line:
                try:
                    return float(line.split(":", 1)[1].strip().split()[0])
                except (IndexError, ValueError):
                    return None
        return None

    def _write_power_budget(self, profile):
        self._write_parameter("platform_mode", profile["platform_mode"])
        self._write_parameter("gpu_tgp", profile["gpu_tgp_offset"])
        self._write_parameter("gpu_dynamic_boost", profile["gpu_dynamic_boost"])
        self._write_parameter("gpu_thermal_target", profile["gpu_thermal_target"])
        self._write_cpu_budget(profile)

    def _write_cpu_budget(self, profile):
        if profile["cpu"] is not None:
            spl, sppt, fppt = profile["cpu"]
            self._write_parameter("cpu_spl", spl)
            self._write_parameter("cpu_sppt", sppt)
            self._write_parameter("cpu_fppt", fppt)

    @staticmethod
    def _valid_cpu_power(values):
        try:
            spl, sppt, fppt = (int(value) for value in values)
        except (TypeError, ValueError):
            return False
        return (
            CPU_SPL_MIN_W <= spl <= CPU_SPL_MAX_W
            and CPU_SPPT_MIN_W <= sppt <= CPU_SPPT_MAX_W
            and CPU_FPPT_MIN_W <= fppt <= CPU_FPPT_MAX_W
            and spl <= sppt <= fppt
        )

    def _write_nvpcf_override(self, profile):
        default_limit, max_limit = self._gpu_power_limits()
        if default_limit is None:
            default_limit = NVIDIA_DEFAULT_TGP_FALLBACK_W
        try:
            base_tgp = int(self._read_text(MODULE_PARAMETERS / "gpu_base_tgp"))
        except ValueError:
            base_tgp = 0

        requested_base = base_tgp + profile["gpu_tgp_offset"]
        ctgp_offset = max(0, round(requested_base - default_limit))
        dynamic_boost = profile["gpu_dynamic_boost"]
        if max_limit is not None:
            dynamic_boost = min(
                dynamic_boost,
                max(0, round(max_limit - default_limit - ctgp_offset)),
            )

        self.nvpcf_override = {
            "enabled": True,
            "default_tgp_w": round(default_limit),
            "base_tgp_w": round(default_limit + ctgp_offset),
            "ctgp_offset_w": ctgp_offset,
            "dynamic_boost_w": dynamic_boost,
            "max_tgp_w": round(default_limit + ctgp_offset + dynamic_boost),
        }

        NVPCF_OVERRIDE_FILE.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        temporary = NVPCF_OVERRIDE_FILE.with_suffix(".tmp")
        temporary.write_text(
            "enabled=1\n"
            f"ctgp_offset_mw={ctgp_offset * 1000}\n"
            f"max_output_offset_mw={dynamic_boost * 1000}\n",
            encoding="ascii",
        )
        temporary.chmod(0o600)
        temporary.replace(NVPCF_OVERRIDE_FILE)
        self.log.info(
            "NVPCF override prepared: base=%.0f+%d W, Dynamic Boost=%d W, max=%.0f W",
            default_limit,
            ctgp_offset,
            dynamic_boost,
            default_limit + ctgp_offset + dynamic_boost,
        )
        return default_limit + ctgp_offset

    def _sync_nvidia_powerd(self, profile):
        # nvidia-powerd reads the firmware TGP/DB budget when it starts. Without
        # a restart it can retain the ceiling from the previous WMI profile.
        action = "restart" if self._on_ac() else "try-restart"
        # The idle ceiling is the configured base TGP. Dynamic Boost raises it
        # only while a GPU-heavy workload leaves enough of the shared budget.
        expected = self._write_nvpcf_override(profile)
        configured_max = self.nvpcf_override["max_tgp_w"]

        last_detail = "power ceiling did not update"
        for attempt in range(2):
            # Let the GPU WMI transaction settle. Reasserting TGP here starts a
            # new firmware negotiation and makes powerd observe the old limit.
            time.sleep(1)
            # Dynamic Boost also evaluates the CPU budget at startup. Repeating
            # only PPT immediately before powerd matches the stable firmware
            # sequence used by Armoury Crate/G-Helper.
            self._write_cpu_budget(profile)
            time.sleep(1)
            subprocess.run(
                ["/usr/bin/systemctl", "reset-failed", "nvidia-powerd.service"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            result = subprocess.run(
                ["/usr/bin/systemctl", action, "nvidia-powerd.service"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                last_detail = result.stderr.strip() or result.stdout.strip()
                continue

            time.sleep(2)
            ceiling = self._gpu_ceiling_limit()
            # NVIDIA reports some mobile limits one watt below the integer
            # firmware value (for example 79 W for an 80 W base ceiling).
            if ceiling is None or ceiling >= expected - 2.0:
                self.log.info(
                    "nvidia-powerd synchronized: base ceiling=%s W, expected=%.0f W, "
                    "dynamic maximum=%d W",
                    "unknown" if ceiling is None else f"{ceiling:.0f}",
                    expected,
                    configured_max,
                )
                return
            last_detail = f"ceiling {ceiling:.0f} W, expected {expected:.0f} W"
            self.log.warning(
                "nvidia-powerd synchronization attempt %d failed: %s",
                attempt + 1,
                last_detail,
            )

        raise Px13PowerError(f"nvidia-powerd synchronization failed: {last_detail}")

    def _set_gpu_offsets(self, core, memory):
        try:
            auth_paths = sorted(
                Path("/run/user").glob("*/.mutter-Xwaylandauth.*"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError as error:
            self.log.info("NVIDIA offsets deferred: Xwayland authority unavailable: %s", error)
            return False
        if not auth_paths:
            self.log.info("NVIDIA offsets deferred: no active Xwayland authority file")
            return False

        environment = {
            "DISPLAY": ":0",
            "XAUTHORITY": str(auth_paths[0]),
        }
        try:
            result = subprocess.run(
                [
                    "/usr/bin/nvidia-settings",
                    "-a",
                    f"[gpu:0]/GPUGraphicsClockOffsetAllPerformanceLevels={core}",
                    "-a",
                    f"[gpu:0]/GPUMemoryTransferRateOffsetAllPerformanceLevels={memory}",
                ],
                check=False,
                capture_output=True,
                env=environment,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            self.log.warning("NVIDIA offset request %s/%s failed: %s", core, memory, error)
            return False
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            self.log.warning("NVIDIA offset request %s/%s failed: %s", core, memory, detail)
            return False
        else:
            self.log.info("NVIDIA offsets applied: core %+d MHz, memory %+d MHz", core, memory)
            self.gpu_core_offset_mhz = core
            self.gpu_memory_offset_mhz = memory
            return True

    def _set_gpu_clock_limit(self, limit):
        if limit == 0:
            command = ["/usr/bin/nvidia-smi", "--reset-gpu-clocks"]
        else:
            command = ["/usr/bin/nvidia-smi", f"--lock-gpu-clocks=0,{limit}"]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            self.log.warning("NVIDIA clock limit request %s failed: %s", limit, error)
            return False
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            self.log.warning("NVIDIA clock limit request %s failed: %s", limit, detail)
            return False

        self.gpu_clock_limit_mhz = limit
        if limit:
            self.log.info("NVIDIA GPU clock limit applied: %d MHz", limit)
        else:
            self.log.info("NVIDIA GPU clock limit reset to default")
        return True

    def _apply_profile(self, name, require_confirmation=True):
        if name not in PROFILES:
            raise Px13PowerError(f"unknown profile: {name}")
        if name != "Safe" and not self._profile_supported(name):
            hardware_max = self._hardware_max_tgp()
            limit = "unavailable" if hardware_max is None else f"{hardware_max} W"
            raise Px13PowerError(
                f"{name} exceeds the GPU firmware maximum ({limit})"
            )
        if name != "Safe" and require_confirmation:
            if not self._on_ac():
                raise Px13PowerError(f"{name} requires AC power")
            if not self._lid_open():
                raise Px13PowerError(f"{name} requires an open lid")
            if name == "120W" and not self.cooling_confirmed:
                raise Px13PowerError("120W requires cooling confirmation")

        profile = dict(PROFILES[name])
        if name in self.cpu_overrides:
            profile["cpu"] = tuple(self.cpu_overrides[name])
        self._set_asus_profile(profile["asus_profile"])
        # asusd reapplies fan curves and cached profile tuning asynchronously.
        # Let that transaction finish before our manual WMI profile becomes the
        # final writer; otherwise its CPU PPT values can win the race.
        time.sleep(2)
        self._set_gpu_offsets(*profile["gpu_offsets"])
        self._set_gpu_clock_limit(profile["gpu_clock_limit_mhz"])
        self._write_power_budget(profile)
        self._set_boost(profile["boost"])
        self._set_cpu_frequency_policy(
            profile["cpu_max_frequency_khz"],
            profile["cpu_epp"],
            profile["cpu_governor"],
        )
        self.active_profile = name
        if not self.resuming:
            self._sync_nvidia_powerd(profile)
        self.last_error = ""
        self._emit_state()

    def _apply_safe(self, reason):
        self.cooling_confirmed = False
        try:
            self._apply_profile("Safe", require_confirmation=False)
        except Px13PowerError as error:
            self.last_error = f"safe fallback failed: {error}"
            self.log.error(self.last_error)
        else:
            self.log.warning("switched to Safe: %s", reason)

    def _emit_state(self):
        GLib.idle_add(self._emit_state_from_main)

    def _emit_state_from_main(self):
        self.StateChanged(self._state_json())
        return False

    def _gpu_ready(self):
        result = subprocess.run(
            ["/usr/bin/nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and bool(result.stdout.strip())

    def _resume_worker(self):
        self.log.info("resume detected; waiting for the NVIDIA driver")
        time.sleep(8)
        ready = 0
        for _ in range(12):
            if self._gpu_ready():
                ready += 1
                if ready == 3:
                    break
            else:
                ready = 0
            time.sleep(2)

        with self.lock:
            self.resuming = False
            self.resume_worker_started = False
            self.cooling_confirmed = False
            if ready != 3:
                self.last_error = "GPU was not ready after resume; Safe retained"
                self._apply_safe(self.last_error)
                self._emit_state()
                return
            self._apply_safe("resume requires renewed cooling confirmation")
            self.log.info("NVIDIA driver stable after resume; leaving nvidia-powerd to NVIDIA's hook")
            self._emit_state()

    def _begin_suspend(self, source):
        with self.lock:
            if self.resuming:
                self.log.info("suspend already handled; source=%s", source)
                return
            self.log.info("suspend detected from %s; applying Safe before sleep", source)
            self.cooling_confirmed = False
            self.resuming = True
            self._apply_safe("system is suspending")

    def _begin_resume(self, source):
        with self.lock:
            if self.resume_worker_started:
                self.log.info("resume already handled; source=%s", source)
                return
            self.log.info("wake detected from %s", source)
            self.cooling_confirmed = False
            self.resuming = True
            self.resume_worker_started = True
        threading.Thread(target=self._resume_worker, daemon=True).start()

    def _prepare_for_sleep(self, sleeping):
        if sleeping:
            self._begin_suspend("logind signal")
        else:
            self._begin_resume("logind signal")

    def _subscribe_sleep(self, bus):
        bus.add_signal_receiver(
            self._prepare_for_sleep,
            signal_name="PrepareForSleep",
            dbus_interface="org.freedesktop.login1.Manager",
        )

    def _guard_worker(self):
        while True:
            time.sleep(1)
            with self.lock:
                high_power = self.nvpcf_override.get("max_tgp_w", 0) > 85
                if high_power and (not self._on_ac() or not self._lid_open()):
                    self.cooling_confirmed = False
                    self._apply_safe("AC or lid safety condition changed")

    def _start_guard(self):
        threading.Thread(target=self._guard_worker, daemon=True).start()

    @dbus.service.method(INTERFACE, in_signature="", out_signature="s")
    def GetState(self):
        with self.lock:
            return self._state_json()

    @dbus.service.method(INTERFACE, in_signature="", out_signature="s")
    def GetCapabilities(self):
        with self.lock:
            return self._capabilities_json()

    @dbus.service.method(INTERFACE, in_signature="", out_signature="s")
    def ConfirmCooling(self):
        with self.lock:
            if not self._on_ac() or not self._lid_open():
                raise dbus.exceptions.DBusException(
                    "org.asuslinux.Px13Power.Error",
                    "Cooling can only be confirmed on AC with the lid open",
                )
            self.cooling_confirmed = True
            self._emit_state()
            return self._state_json()

    @dbus.service.method(INTERFACE, in_signature="", out_signature="s")
    def PrepareSuspend(self):
        self._begin_suspend("system-sleep hook")
        with self.lock:
            return self._state_json()

    @dbus.service.method(INTERFACE, in_signature="", out_signature="s")
    def ResumeAfterSleep(self):
        self._begin_resume("system-sleep hook")
        with self.lock:
            return self._state_json()

    @dbus.service.method(INTERFACE, in_signature="s", out_signature="s")
    def Activate(self, profile):
        profile = str(profile)
        with self.lock:
            try:
                self._apply_profile(profile)
                if profile == "Safe":
                    self.cooling_confirmed = False
                self.requested_profile = profile
                self._persist_state()
            except Px13PowerError as error:
                self.last_error = str(error)
                self._emit_state()
                raise dbus.exceptions.DBusException("org.asuslinux.Px13Power.Error", str(error))
            return self._state_json()

    @dbus.service.method(INTERFACE, in_signature="iii", out_signature="s")
    def SetGpuPower(self, base_power, dynamic_boost, temperature_target):
        base_power = int(base_power)
        dynamic_boost = int(dynamic_boost)
        temperature_target = int(temperature_target)
        with self.lock:
            try:
                base_tgp = int(self._read_text(MODULE_PARAMETERS / "gpu_base_tgp"))
            except ValueError as error:
                raise dbus.exceptions.DBusException(
                    "org.asuslinux.Px13Power.Error", "GPU base TGP is unavailable"
                ) from error

            hardware_max = self._hardware_max_tgp()
            if hardware_max is None:
                raise dbus.exceptions.DBusException(
                    "org.asuslinux.Px13Power.Error",
                    "GPU firmware maximum is unavailable; refusing power tuning",
                )
            maximum_base = min(base_tgp + 40, hardware_max - 5)

            valid = (
                base_tgp <= base_power <= maximum_base
                and 5 <= dynamic_boost <= 25
                and 75 <= temperature_target <= 87
                and base_power + dynamic_boost <= hardware_max
            )
            if not valid:
                raise dbus.exceptions.DBusException(
                    "org.asuslinux.Px13Power.Error",
                    f"GPU power must fit base {base_tgp}..{maximum_base} W, "
                    f"boost 5..25 W, temperature 75..87 C, and {hardware_max} W total",
                )
            if not self._on_ac() or not self._lid_open() or not self.cooling_confirmed:
                raise dbus.exceptions.DBusException(
                    "org.asuslinux.Px13Power.Error",
                    "GPU power tuning requires AC power, an open lid, and cooling confirmation",
                )

            profile = dict(PROFILES["120W"])
            profile["gpu_tgp_offset"] = base_power - base_tgp
            profile["gpu_dynamic_boost"] = dynamic_boost
            profile["gpu_thermal_target"] = temperature_target
            profile["gpu_offsets"] = (self.gpu_core_offset_mhz, self.gpu_memory_offset_mhz)
            profile["gpu_clock_limit_mhz"] = self.gpu_clock_limit_mhz

            try:
                self._set_asus_profile("Performance")
                time.sleep(2)
                self._write_power_budget(profile)
                self._set_boost(profile["boost"])
                self._set_cpu_frequency_policy(
                    profile["cpu_max_frequency_khz"],
                    profile["cpu_epp"],
                    profile["cpu_governor"],
                )
                self.active_profile = "Custom"
                self._sync_nvidia_powerd(profile)
            except Px13PowerError as error:
                self.last_error = str(error)
                self._emit_state()
                raise dbus.exceptions.DBusException(
                    "org.asuslinux.Px13Power.Error", str(error)
                ) from error

            self.last_error = ""
            self._emit_state()
            return self._state_json()

    @dbus.service.method(INTERFACE, in_signature="iii", out_signature="s")
    def SetCpuPower(self, spl, sppt, fppt):
        values = (int(spl), int(sppt), int(fppt))
        with self.lock:
            if not self._valid_cpu_power(values):
                raise dbus.exceptions.DBusException(
                    "org.asuslinux.Px13Power.Error",
                    "CPU power must fit SPL 5..45 W, SPPT 5..55 W, FPPT 5..65 W, "
                    "with SPL <= SPPT <= FPPT",
                )
            if self.active_profile == "Safe":
                raise dbus.exceptions.DBusException(
                    "org.asuslinux.Px13Power.Error",
                    "CPU power tuning requires an active performance profile",
                )
            if not self._on_ac() or not self._lid_open() or not self.cooling_confirmed:
                raise dbus.exceptions.DBusException(
                    "org.asuslinux.Px13Power.Error",
                    "CPU power tuning requires AC power, an open lid, and cooling confirmation",
                )

            old_values = tuple(
                int(self._read_text(MODULE_PARAMETERS / name))
                for name in ("cpu_spl", "cpu_sppt", "cpu_fppt")
            )
            try:
                self._write_cpu_budget({"cpu": values})
                self._set_boost(True)
            except (Px13PowerError, ValueError) as error:
                try:
                    self._write_cpu_budget({"cpu": old_values})
                except Px13PowerError:
                    self.log.exception("CPU power rollback failed")
                self.last_error = str(error)
                self._emit_state()
                raise dbus.exceptions.DBusException(
                    "org.asuslinux.Px13Power.Error", str(error)
                ) from error

            target_profile = self.active_profile
            if target_profile not in PROFILES:
                target_profile = self.requested_profile
            if target_profile in PROFILES and target_profile != "Safe":
                self.cpu_overrides[target_profile] = list(values)
                self._persist_state()
            self.last_error = ""
            self._emit_state()
            return self._state_json()

    @dbus.service.method(INTERFACE, in_signature="ii", out_signature="s")
    def SetGpuOffsets(self, core, memory):
        core = int(core)
        memory = int(memory)
        with self.lock:
            if self.active_profile == "Safe":
                raise dbus.exceptions.DBusException(
                    "org.asuslinux.Px13Power.Error",
                    "GPU clock offsets require an active performance profile",
                )
            if not self._on_ac() or not self._lid_open() or not self.cooling_confirmed:
                raise dbus.exceptions.DBusException(
                    "org.asuslinux.Px13Power.Error",
                    "GPU clock offsets require AC power, an open lid, and cooling confirmation",
                )
            if not (
                GPU_CORE_OFFSET_MIN_MHZ <= core <= GPU_CORE_OFFSET_MAX_MHZ
                and GPU_MEMORY_OFFSET_MIN_MHZ <= memory <= GPU_MEMORY_OFFSET_MAX_MHZ
            ):
                raise dbus.exceptions.DBusException(
                    "org.asuslinux.Px13Power.Error",
                    "GPU offsets are limited to core -1000..1000 MHz and memory -2000..6000 MHz",
                )
            if not self._set_gpu_offsets(core, memory):
                self.last_error = "NVIDIA rejected the GPU clock offset request"
                self._emit_state()
                raise dbus.exceptions.DBusException(
                    "org.asuslinux.Px13Power.Error", self.last_error
                )
            self.last_error = ""
            self._emit_state()
            return self._state_json()

    @dbus.service.method(INTERFACE, in_signature="i", out_signature="s")
    def SetGpuClockLimit(self, limit):
        limit = int(limit)
        with self.lock:
            if self.active_profile == "Safe":
                raise dbus.exceptions.DBusException(
                    "org.asuslinux.Px13Power.Error",
                    "GPU clock limit requires an active performance profile",
                )
            if not self._on_ac() or not self._lid_open() or not self.cooling_confirmed:
                raise dbus.exceptions.DBusException(
                    "org.asuslinux.Px13Power.Error",
                    "GPU clock limit requires AC power, an open lid, and cooling confirmation",
                )
            valid_limit = limit == 0 or (
                GPU_CLOCK_LIMIT_MIN_MHZ <= limit <= GPU_CLOCK_LIMIT_MAX_MHZ
                and limit % GPU_CLOCK_LIMIT_STEP_MHZ == 0
            )
            if not valid_limit:
                raise dbus.exceptions.DBusException(
                    "org.asuslinux.Px13Power.Error",
                    "GPU clock limit must be 0 (default) or 210..3105 MHz in 15 MHz steps",
                )
            if not self._set_gpu_clock_limit(limit):
                self.last_error = "NVIDIA rejected the GPU clock limit request"
                self._emit_state()
                raise dbus.exceptions.DBusException(
                    "org.asuslinux.Px13Power.Error", self.last_error
                )
            self.last_error = ""
            self._emit_state()
            return self._state_json()

    @dbus.service.signal(INTERFACE, signature="s")
    def StateChanged(self, state):
        pass

    def initialize(self):
        with self.lock:
            self.cooling_confirmed = False
            self._apply_safe("daemon startup")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    daemon = Px13Power(bus)
    daemon.initialize()
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
