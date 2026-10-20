# SPDX-License-Identifier: MIT
"""Power policy: profiles, manual tuning and safety fallbacks.

The controller is the single owner of the WMI power budget, the CPU policy and
the NVIDIA power settings. It knows nothing about D-Bus. Requests it refuses
raise RequestError and change nothing; failed hardware operations raise
HardwareError and are also reported as last_error.
"""

import json
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from .balance import BatteryBalance
from .config import REQUIREMENTS, Range, describe_missing
from .hardware import HardwareError
from .nvidia import nvpcf_policy, write_nvpcf_override

STATE_FILE = Path("/var/lib/px13-powerd/state.json")
NVPCF_OVERRIDE_FILE = Path("/run/px13-powerd/nvpcf-override.conf")
GPU_KEYS = ("gpu_tgp_offset", "gpu_dynamic_boost", "gpu_thermal_target")
# asusd reapplies fan curves and cached tuning asynchronously after a profile
# change. Waiting lets the controller's WMI budget be the final write.
ASUSD_SETTLE_S = 2
CLOCK_RESET_RETRY_S = 60
SOURCE_DEBOUNCE_S = 3


class RequestError(ValueError):
    """A request the controller refuses; nothing was changed."""


class Controller:
    def __init__(self, model, hw, log, state_file=STATE_FILE, nvpcf_file=NVPCF_OVERRIDE_FILE,
                 sleep=time.sleep, now=time.monotonic):
        self.model = model
        self.hw = hw
        self.log = log
        self.state_file = Path(state_file)
        self.nvpcf_file = Path(nvpcf_file)
        self._sleep = sleep
        self._now = now
        self.lock = threading.RLock()
        self.on_change = lambda: None

        self.active_profile = self.requested_profile = model.default_profile
        self.cooling_confirmed = False
        self.last_error = ""
        self.resuming = False
        self._resume_started = False
        self._applied_source = None
        self._source_change_time = None
        self.overrides = {}  # profile -> manual cpu/GPU values, persisted
        self.nvpcf = {}
        self.balance = None
        # The driver state of offsets and the clock lock is unknown until the
        # first successful write. Resets are deferred while the dGPU sleeps.
        self.gpu_offsets = (0, 0)
        self.gpu_clock_limit = 0
        self._offsets_known = False
        self._clock_limit_known = False
        self._clock_reset_time = 0.0
        self._gpu_limits = None  # cached VBIOS (default, maximum) W
        self._load()

    # Conditions and requirements

    def _conditions(self):
        source = self.hw.supplies.source()
        return {
            "source": source,
            "ac": source != "battery",
            "adapter": source == "adapter",
            "lid_open": self.hw.lid_open(),
            "cooling_confirmation": self.cooling_confirmed,
        }

    @staticmethod
    def _missing(requires, conditions):
        return [key for key in REQUIREMENTS if key in requires and not conditions[key]]

    def _tuning_blocked(self, conditions):
        """Why manual tuning is not allowed now, or an empty string."""
        if self.active_profile == self.model.default_profile:
            return f"Choose a profile other than {self.model.default_profile} to change limits."
        missing = self._missing(self.model.tuning_requires, conditions)
        return f"Changing limits requires {describe_missing(missing)}." if missing else ""

    def _require_tuning(self):
        reason = self._tuning_blocked(self._conditions())
        if reason:
            raise RequestError(reason)

    # GPU firmware limits

    def _gpu_power_limits(self):
        # The limits come from the VBIOS and do not change at runtime. They
        # are cached so that capability queries and battery transitions do
        # not wake a suspended dGPU.
        if self._gpu_limits is not None:
            return tuple(self._gpu_limits)
        if self.hw.nvidia.suspended() and self.hw.supplies.source() == "battery":
            return None, None
        default, maximum = self.hw.nvidia.power_limits()
        if default is not None and 0 < default <= maximum:
            self._gpu_limits = [default, maximum]
            self._persist()
        return default, maximum

    def _hardware_max_tgp(self):
        _, maximum = self._gpu_power_limits()
        return None if maximum is None or maximum <= 0 else int(maximum + 0.001)

    def _supported(self, profile, hardware_max):
        # Profiles without requirements stay within the firmware default
        # envelope and must work even before the VBIOS limits are known.
        if not profile.requires:
            return True
        base = self.hw.wmi.base_tgp()
        return (
            base is not None
            and hardware_max is not None
            and base + profile.gpu_tgp_offset + profile.gpu_dynamic_boost <= hardware_max
        )

    def _gpu_power_ranges(self, base_tgp, hardware_max):
        gpu = self.model.gpu
        return {
            "base": Range(
                base_tgp + gpu.tgp_offset.min,
                min(base_tgp + gpu.tgp_offset.max, hardware_max - gpu.dynamic_boost.min),
            ),
            "boost": gpu.dynamic_boost,
            "temperature": gpu.temperature,
        }

    # Applying profiles

    def _resolve(self, name, source):
        profile = self.model.profiles[name].for_source(source)
        override = dict(self.overrides.get(name, {}))
        if "cpu" in override:
            override["cpu"] = tuple(override["cpu"])
        return replace(profile, **override)

    def _apply_profile(self, name, check_requirements=True):
        profile = self.model.profiles.get(name)
        if profile is None:
            raise RequestError(f"unknown profile: {name}")
        maximum = self._hardware_max_tgp()
        if not self._supported(profile, maximum):
            raise RequestError(
                f"{name} exceeds the GPU firmware maximum ({'unavailable' if maximum is None else f'{maximum} W'})"
            )
        conditions = self._conditions()
        missing = self._missing(profile.requires, conditions)
        if check_requirements and missing:
            raise RequestError(f"{name} requires {describe_missing(missing)}")

        source = conditions["source"]
        resolved = self._resolve(name, source)
        self.hw.set_asus_profile(resolved.asus_profile)
        self._sleep(ASUSD_SETTLE_S)
        # Profiles never overclock; offsets and the clock limit are opt-in.
        self._apply_gpu_clocks((0, 0), 0)
        self.hw.cpu.set_boost(True)
        self.hw.cpu.set_policy(resolved.cpu_epp)
        self.hw.wmi.write_power_budget(resolved)
        self.active_profile = name
        self._applied_source = source
        self.balance = (
            BatteryBalance(self.model.balance, self.model.gpu.clock_limit.step, resolved.cpu[0], self._now())
            if resolved.battery_balance
            else None
        )
        self._update_nvidia(resolved, source)
        self.last_error = ""

    def _update_nvidia(self, profile, source):
        if self.resuming:
            return
        if source == "battery":
            # On battery the firmware caps the dGPU (45 W on the PX13) and
            # disables Dynamic Boost, and asusd may stop nvidia-powerd. Keep
            # the override current for its next start instead of restarting
            # it and waking the dGPU to verify an unreachable ceiling.
            self._write_nvpcf(profile)
        else:
            self._sync_nvidia_powerd(profile)

    def _write_nvpcf(self, profile):
        default, maximum = self._gpu_power_limits()
        self.nvpcf = nvpcf_policy(
            default or self.model.gpu.default_tgp_w, maximum, self.hw.wmi.base_tgp() or 0, profile
        )
        write_nvpcf_override(self.nvpcf_file, self.nvpcf)
        self.log.info(
            "NVPCF override prepared: base=%d W, Dynamic Boost=%d W, max=%d W",
            self.nvpcf["base_tgp_w"],
            self.nvpcf["dynamic_boost_w"],
            self.nvpcf["max_tgp_w"],
        )
        return self.nvpcf["base_tgp_w"]

    def _sync_nvidia_powerd(self, profile):
        # nvidia-powerd reads the firmware TGP/Dynamic Boost budget when it
        # starts. Without a restart it can keep the previous ceiling. The idle
        # ceiling is the base TGP; Dynamic Boost raises it under GPU load.
        expected = self._write_nvpcf(profile)
        detail = "power ceiling did not update"
        for attempt in range(2):
            # Let the GPU WMI transaction settle. Reasserting TGP here starts
            # a new firmware negotiation and powerd observes the old limit.
            self._sleep(1)
            # Dynamic Boost also evaluates the CPU budget at startup. Repeating
            # only PPT right before powerd matches the stable firmware sequence
            # used by Armoury Crate and G-Helper.
            self.hw.wmi.write_cpu_power(profile.cpu)
            self._sleep(1)
            ok, detail = self.hw.nvidia.restart_powerd("restart")
            if not ok:
                continue
            self._sleep(2)
            ceiling = self.hw.nvidia.ceiling_limit()
            # NVIDIA reports some mobile limits one watt below the firmware
            # value, for example 79 W for an 80 W ceiling.
            if ceiling is None or ceiling >= expected - 2.0:
                self.log.info(
                    "nvidia-powerd synchronized: ceiling=%s W, expected=%d W, maximum=%d W",
                    "unknown" if ceiling is None else f"{ceiling:.0f}",
                    expected,
                    self.nvpcf["max_tgp_w"],
                )
                return
            detail = f"ceiling {ceiling:.0f} W, expected {expected} W"
            self.log.warning("nvidia-powerd synchronization attempt %d failed: %s", attempt + 1, detail)
        raise HardwareError(f"nvidia-powerd synchronization failed: {detail}")

    # GPU clocks

    def _set_offsets(self, core, memory):
        if not self.hw.nvidia.set_offsets(core, memory):
            return False
        self.gpu_offsets = (core, memory)
        self._offsets_known = True
        return True

    def _set_clock_limit(self, limit):
        if not self.hw.nvidia.set_clock_limit(limit):
            return False
        self.gpu_clock_limit = limit
        self._clock_limit_known = True
        return True

    def _clock_reset_pending(self):
        return not (
            self._offsets_known
            and self._clock_limit_known
            and self.gpu_offsets == (0, 0)
            and self.gpu_clock_limit == 0
        )

    def _apply_gpu_clocks(self, offsets, limit):
        # Returning to defaults does not need an awake GPU: a suspended dGPU
        # runs no clocks, and the guard resets them once something else has
        # woken it. Only explicit tuning wakes the GPU immediately.
        if offsets == (0, 0) and limit == 0 and self.hw.nvidia.suspended():
            if self._clock_reset_pending():
                self.log.info("NVIDIA clock reset deferred until the dGPU is awake")
            return
        if not (self._offsets_known and self.gpu_offsets == offsets):
            self._set_offsets(*offsets)
        if not (self._clock_limit_known and self.gpu_clock_limit == limit):
            self._set_clock_limit(limit)

    # Fallbacks, suspend and resume

    def _notify(self):
        self.on_change()

    @contextmanager
    def _reporting(self, context=""):
        """Records hardware failures as last_error before passing them on."""
        try:
            yield
        except HardwareError as error:
            self.last_error = f"{context}{error}"
            self.log.error(self.last_error)
            self._notify()
            raise

    def _fallback(self, reason):
        self.cooling_confirmed = False
        self._source_change_time = None
        try:
            with self._reporting(f"{self.model.default_profile} fallback failed: "):
                self._apply_profile(self.model.default_profile, check_requirements=False)
        except HardwareError:
            return
        self.log.warning("switched to %s: %s", self.model.default_profile, reason)
        self._notify()

    def _restore_requested(self):
        """Restores a requested profile that needs no conditions."""
        name = self.requested_profile
        profile = self.model.profiles.get(name)
        if name == self.model.default_profile or profile is None or profile.requires:
            return
        try:
            with self._reporting(f"{name} restore failed: "):
                self._apply_profile(name, check_requirements=False)
        except HardwareError:
            return
        self.log.info("restored %s", name)

    def _gpu_ready(self):
        if self.hw.supplies.source() == "battery":
            # On battery nothing needs the dGPU after resume. A bound driver
            # is enough; querying NVML would wake the GPU from D3cold.
            return self.hw.nvidia.driver_bound()
        return self.hw.nvidia.responds()

    def _resume_worker(self):
        self.log.info("resume detected; waiting for the NVIDIA driver")
        self._sleep(8)
        ready = 0
        for _ in range(12):
            ready = ready + 1 if self._gpu_ready() else 0
            if ready == 3:
                break
            self._sleep(2)
        with self.lock:
            self.resuming = False
            self._resume_started = False
            if ready != 3:
                self._fallback("GPU was not ready after resume")
                self.last_error = f"GPU was not ready after resume; {self.model.default_profile} retained"
            else:
                self._fallback("resume requires renewed cooling confirmation")
                self._restore_requested()
            self._notify()

    def prepare_suspend(self, source):
        with self.lock:
            if self.resuming:
                self.log.info("suspend already handled; source=%s", source)
            else:
                self.log.info("suspend detected from %s", source)
                self.resuming = True
                self._fallback("system is suspending")
            return self.state()

    def resume(self, source):
        with self.lock:
            if self._resume_started:
                self.log.info("resume already handled; source=%s", source)
                return self.state()
            self.log.info("wake detected from %s", source)
            self.cooling_confirmed = False
            self.resuming = True
            self._resume_started = True
        threading.Thread(target=self._resume_worker, daemon=True).start()
        with self.lock:
            return self.state()

    def _balance_step(self, now):
        targets = self.balance.update(
            now, self.hw.supplies.battery_energy_wh(), self.hw.supplies.battery_full()
        )
        if targets is None:
            return
        spl, clock = targets
        try:
            if spl != self.balance.spl:
                self.hw.wmi.write_cpu_power(BatteryBalance.cpu_power(spl))
                self.balance.spl = spl
            if self.balance.wants_clock(clock) and self._set_clock_limit(clock):
                self.balance.gpu_clock = clock
        except HardwareError as error:
            self.last_error = f"battery balance failed: {error}"
            self.log.error(self.last_error)
        self._notify()

    def guard_tick(self):
        """Runs once a second: enforces requirements and follows the power source."""
        with self.lock:
            now = self._now()
            conditions = self._conditions()
            profile = self.model.profiles[self.active_profile]
            # Cooling confirmation is checked when a profile is chosen; it
            # cannot become false on its own.
            missing = self._missing(profile.requires - {"cooling_confirmation"}, conditions)
            if missing:
                self._fallback(f"{self.active_profile} requires {describe_missing(missing)}")
                return
            if self.resuming:
                self._source_change_time = None
                return
            if self.balance is not None:
                if conditions["ac"]:
                    self._balance_step(now)
                else:
                    self.balance.forget()
            source = conditions["source"]
            if profile.for_source(source) != profile.for_source(self._applied_source):
                # asusd switches its platform profile on the same event; wait
                # for it so that the battery or AC variant is the final write.
                if self._source_change_time is None:
                    self._source_change_time = now
                elif now - self._source_change_time >= SOURCE_DEBOUNCE_S:
                    self._source_change_time = None
                    try:
                        with self._reporting("power source change failed: "):
                            self._apply_profile(self.active_profile, check_requirements=False)
                    except HardwareError:
                        pass
                    self._notify()
                return
            self._source_change_time = None
            if (
                self.active_profile == self.model.default_profile
                and self._clock_reset_pending()
                and now - self._clock_reset_time >= CLOCK_RESET_RETRY_S
                and not self.hw.nvidia.suspended()
            ):
                self._clock_reset_time = now
                self._apply_gpu_clocks((0, 0), 0)

    def start(self):
        with self.lock:
            self.hw.cpu.ensure_active_pstate(self.log)
            # nvidia-powerd may start before the first profile is applied.
            self._write_nvpcf(self.model.profiles[self.model.default_profile])
            self._fallback("daemon startup")
            self._restore_requested()

        def guard():
            while True:
                self._sleep(1)
                self.guard_tick()

        threading.Thread(target=guard, daemon=True).start()

    # Requests

    def _override(self, name):
        return self.overrides.setdefault(name, {})

    def activate(self, name):
        with self.lock, self._reporting():
            self._apply_profile(name)
            if name == self.model.default_profile:
                self.cooling_confirmed = False
            self.requested_profile = name
            self._persist()
            self._notify()
            return self.state()

    def reset(self):
        """Drops manual changes of the active profile and applies it again."""
        with self.lock, self._reporting():
            self.overrides.pop(self.active_profile, None)
            self._persist()
            self._apply_profile(self.active_profile, check_requirements=False)
            self._notify()
            return self.state()

    def confirm_cooling(self):
        with self.lock:
            conditions = self._conditions()
            if not (conditions["ac"] and conditions["lid_open"]):
                raise RequestError("Cooling can only be confirmed on external power with the lid open")
            self.cooling_confirmed = True
            self._notify()
            return self.state()

    def set_cpu_power(self, sustained, slow, fast):
        values = (int(sustained), int(slow), int(fast))
        with self.lock:
            if not self.model.cpu.accepts(values):
                raise RequestError(f"CPU power must fit {self.model.cpu.describe()}")
            self._require_tuning()
            previous = self.hw.wmi.cpu_power()
            with self._reporting():
                try:
                    self.hw.wmi.write_cpu_power(values)
                    self.hw.cpu.set_boost(True)
                except HardwareError:
                    if None not in previous:
                        try:
                            self.hw.wmi.write_cpu_power(previous)
                        except HardwareError:
                            self.log.exception("CPU power rollback failed")
                    raise
            if self.balance is not None:
                self.balance.set_ceiling(values[0])
            self._override(self.active_profile)["cpu"] = list(values)
            self._persist()
            self.last_error = ""
            self._notify()
            return self.state()

    def set_gpu_power(self, base, boost, temperature):
        base, boost, temperature = int(base), int(boost), int(temperature)
        with self.lock:
            self._require_tuning()
            base_tgp = self.hw.wmi.base_tgp()
            hardware_max = self._hardware_max_tgp()
            if base_tgp is None or hardware_max is None:
                raise RequestError("GPU base TGP or firmware maximum is unavailable; refusing power tuning")
            ranges = self._gpu_power_ranges(base_tgp, hardware_max)
            if not (
                base in ranges["base"]
                and boost in ranges["boost"]
                and temperature in ranges["temperature"]
                and base + boost <= hardware_max
            ):
                raise RequestError(
                    f"GPU power must fit base {ranges['base'].describe('W')}, "
                    f"boost {ranges['boost'].describe('W')}, "
                    f"temperature {ranges['temperature'].describe('C')}, "
                    f"and {hardware_max} W in total"
                )
            values = dict(zip(GPU_KEYS, (base - base_tgp, boost, temperature), strict=True))
            source = self.hw.supplies.source()
            resolved = replace(self._resolve(self.active_profile, source), **values)
            with self._reporting():
                self.hw.wmi.write_power_budget(resolved)
                self._update_nvidia(resolved, source)
            self._override(self.active_profile).update(values)
            self._persist()
            self.last_error = ""
            self._notify()
            return self.state()

    def set_gpu_offsets(self, core, memory):
        core, memory = int(core), int(memory)
        gpu = self.model.gpu
        with self.lock:
            if core not in gpu.core_offset or memory not in gpu.memory_offset:
                raise RequestError(
                    f"GPU offsets must fit core {gpu.core_offset.describe('MHz')} "
                    f"and memory {gpu.memory_offset.describe('MHz')}"
                )
            self._require_tuning()
            with self._reporting():
                if not self._set_offsets(core, memory):
                    raise HardwareError("NVIDIA rejected the GPU clock offset request")
            self.last_error = ""
            self._notify()
            return self.state()

    def set_gpu_clock_limit(self, limit):
        limit = int(limit)
        clock_limit = self.model.gpu.clock_limit
        with self.lock:
            if limit != 0 and limit not in clock_limit:
                raise RequestError(f"GPU clock limit must be 0 (default) or {clock_limit.describe('MHz')}")
            self._require_tuning()
            with self._reporting():
                if not self._set_clock_limit(limit):
                    raise HardwareError("NVIDIA rejected the GPU clock limit request")
            self.last_error = ""
            self._notify()
            return self.state()

    # Reports

    def capabilities(self):
        with self.lock:
            model, wmi = self.model, self.hw.wmi
            hardware_max = self._hardware_max_tgp()
            caps = {
                "name": model.name,
                "default_profile": model.default_profile,
                "profiles": [
                    name for name, profile in model.profiles.items() if self._supported(profile, hardware_max)
                ],
                "requires_cooling_confirmation": model.requires_cooling_confirmation,
                "gpu_clocks": {
                    "core_offset": model.gpu.core_offset.to_json(),
                    "memory_offset": model.gpu.memory_offset.to_json(),
                    "limit": model.gpu.clock_limit.to_json(),
                },
            }
            if wmi.has_cpu_power():
                caps["cpu_power"] = {
                    "sustained": model.cpu.sustained.to_json(),
                    "slow": model.cpu.slow.to_json(),
                    "fast": model.cpu.fast.to_json(),
                }
            base_tgp = wmi.base_tgp()
            if base_tgp and hardware_max and hardware_max >= base_tgp + model.gpu.dynamic_boost.min:
                ranges = self._gpu_power_ranges(base_tgp, hardware_max)
                caps["gpu_power"] = {key: value.to_json() for key, value in ranges.items()}
                caps["gpu_power"]["max_total"] = hardware_max
            return caps

    def state(self):
        with self.lock:
            conditions = self._conditions()
            blocked = self._tuning_blocked(conditions)
            wmi = self.hw.wmi.values()
            state = {
                "active_profile": self.active_profile,
                "requested_profile": self.requested_profile,
                "controls_ready": not blocked,
                "blocked_reason": blocked,
                # The battery balance controller owns the clock limit.
                "clocks_enabled": self.balance is None,
                "cooling_confirmed": self.cooling_confirmed,
                "cooling_confirmable": conditions["ac"] and conditions["lid_open"],
                "gpu_clocks": {
                    "core_offset_mhz": self.gpu_offsets[0],
                    "memory_offset_mhz": self.gpu_offsets[1],
                    "limit_mhz": self.gpu_clock_limit,
                },
                "telemetry": {
                    **self.hw.nvidia.telemetry(),
                    "cpu_temperature_c": self.hw.cpu.temperature_c(),
                    "cpu_frequency_mhz": self.hw.cpu.frequency_mhz(),
                },
                "last_error": self.last_error,
                # Diagnostics for px13ctl.
                "power_source": conditions["source"],
                "lid_open": conditions["lid_open"],
                "cpu_boost": self.hw.cpu.boost_active(),
                "resuming": self.resuming,
                "battery_balance": None if self.balance is None else self.balance.to_json(),
                "nvpcf_override": self.nvpcf,
                "wmi": wmi,
                "cpu_policy": self.hw.cpu.info(),
            }
            if None not in (wmi["cpu_spl"], wmi["cpu_sppt"], wmi["cpu_fppt"]):
                state["cpu_power"] = {
                    "sustained_w": wmi["cpu_spl"],
                    "slow_w": wmi["cpu_sppt"],
                    "fast_w": wmi["cpu_fppt"],
                }
            if None not in (wmi["gpu_base_tgp"], wmi["gpu_tgp"], wmi["gpu_dynamic_boost"], wmi["gpu_thermal_target"]):
                state["gpu_power"] = {
                    "base_w": wmi["gpu_base_tgp"] + wmi["gpu_tgp"],
                    "boost_w": wmi["gpu_dynamic_boost"],
                    "temperature_c": wmi["gpu_thermal_target"],
                }
            return state

    # Persistence

    def _valid_override(self, override):
        try:
            if set(override) - {"cpu", *GPU_KEYS}:
                return False
            if "cpu" in override and not self.model.cpu.accepts(tuple(override["cpu"])):
                return False
            gpu = self.model.gpu
            ranges = dict(zip(GPU_KEYS, (gpu.tgp_offset, gpu.dynamic_boost, gpu.temperature), strict=True))
            return all(override[key] in ranges[key] for key in GPU_KEYS if key in override)
        except (TypeError, ValueError):
            return False

    def _load(self):
        try:
            data = json.loads(self.state_file.read_text(encoding="ascii"))
        except (OSError, ValueError):
            return
        if data.get("requested_profile") in self.model.profiles:
            self.requested_profile = data["requested_profile"]
        for name, override in data.get("overrides", {}).items():
            if name in self.model.profiles and name != self.model.default_profile and self._valid_override(override):
                self.overrides[name] = override
        limits = data.get("gpu_limits")
        try:
            default, maximum = (float(value) for value in limits)
        except (TypeError, ValueError):
            return
        if 0 < default <= maximum:
            self._gpu_limits = [default, maximum]

    def _persist(self):
        try:
            self.state_file.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            temporary = self.state_file.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    {
                        "requested_profile": self.requested_profile,
                        "overrides": self.overrides,
                        "gpu_limits": self._gpu_limits,
                    },
                    sort_keys=True,
                ),
                encoding="ascii",
            )
            temporary.replace(self.state_file)
        except OSError as error:
            self.log.warning("state file write failed: %s", error)
