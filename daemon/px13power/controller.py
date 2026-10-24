# SPDX-License-Identifier: MIT
"""Power policy: modes, profiles, manual tuning and safety.

The controller is the single owner of the WMI power budget, the CPU policy and
the NVIDIA power settings. It follows the platform profile: each mode uses the
first of its profiles whose requirements hold. It knows nothing about D-Bus.

Requests return at once. Quick writes happen immediately; slow steps (waiting
for asusd, restarting nvidia-powerd) run on one worker thread, where newer work
supersedes older work. Every hardware write derives its values from the
current desired state under the lock, and no wait holds the lock. NVML can
take seconds while it wakes the dGPU, so NVML calls never run under it; GPU
clock changes are serialized by a separate clock lock, which is always taken
before the state lock, never inside it.

Requests the controller refuses raise RequestError and change nothing; failed
hardware operations raise HardwareError and are reported as last_error.
"""

import json
import threading
import time
from dataclasses import replace
from pathlib import Path

from .balance import BatteryBalance
from .battery import BatteryMonitor
from .config import REQUIREMENTS, Range, describe_missing
from .hardware import HardwareError
from .nvidia import NvidiaError, nvpcf_policy, write_nvpcf_override

STATE_FILE = Path("/var/lib/px13-powerd/state.json")
NVPCF_OVERRIDE_FILE = Path("/run/px13-powerd/nvpcf-override.conf")
GPU_KEYS = ("gpu_tgp_offset", "gpu_dynamic_boost", "gpu_thermal_target")
CHARGER_TOO_WEAK = "The battery drains on external power: the charger does not cover the load"
# A USB-C PD contract can get stuck with the input off while the firmware still
# reports "Charging"; only a re-plug restores it (docs/FINDINGS.md).
USB_C_HINT = ". Under a light load, re-plug the USB-C charger"
# A critical battery that keeps draining on external power puts the system to
# sleep after this long. The firmware still reports external power and
# "Charging" then, so the desktop's own low-battery action does not trigger.
RESCUE_DELAY_S = 60
# asusd reapplies fan curves and cached tuning asynchronously after a mode
# change. Waiting lets the controller's WMI budget be the final write.
ASUSD_SETTLE_S = 2
CLOCK_RESET_RETRY_S = 60
SYNC_ATTEMPTS = 2
IDLE_TIMEOUT_S = 30

# Pending work, each level including the ones below it.
SYNC_GPU = 1  # NVPCF override and nvidia-powerd
APPLY_PROFILE = 2  # CPU policy, WMI budget, clocks, then SYNC_GPU


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
        self._changed = threading.Condition(self.lock)
        self._clock_lock = threading.Lock()
        self.on_change = lambda: None

        self.mode = None
        self.source = None
        self.profile = model.default_profile
        self.cooling_confirmed = False
        self.holding = False  # suspend or shutdown: the default profile only
        self.last_error = ""
        self.overrides = {}  # profile -> manual CPU/GPU values, persisted
        self.nvpcf = {}
        self.balance = None
        self.monitor = BatteryMonitor()
        self._weak_since = None  # critical and draining on external power
        self._rescued = False
        self.gpu_offsets = (0, 0)
        self.gpu_clock_limit = 0
        # The driver's clock state is unknown at startup. A reset is deferred
        # while the dGPU sleeps: it runs no clocks then.
        self._clock_reset_pending = True
        self._clock_reset_time = None
        self._gpu_info = None  # static NVML limits, persisted
        self._pending = 0
        self._generation = 0
        self._busy = False
        self._settle_until = 0.0
        self._worker = None
        self._resume_started = False
        self._load()

    # Conditions and selection

    def _conditions(self):
        source = self.hw.supplies.source()
        return {
            "mode": self.hw.platform_profile(),
            "source": source,
            "ac": source != "battery",
            "adapter": source == "adapter",
            "lid_open": self.hw.lid_open(),
            "cooling_confirmation": self.cooling_confirmed,
        }

    @staticmethod
    def _missing(requires, conditions):
        return [key for key in REQUIREMENTS if key in requires and not conditions[key]]

    def _supported(self, profile):
        # Profiles without requirements stay within the firmware default
        # envelope and must work even before the VBIOS limits are known.
        if not profile.requires:
            return True
        base, maximum = self.hw.wmi.base_tgp(), self._hardware_max_tgp()
        return (
            base is not None
            and maximum is not None
            and base + profile.gpu_tgp_offset + profile.gpu_dynamic_boost <= maximum
        )

    def _select(self, conditions):
        if self.holding:
            return self.model.default_profile
        for name in self.model.modes.get(conditions["mode"], ()):
            profile = self.model.profiles[name]
            if self._supported(profile) and not self._missing(profile.requires, conditions):
                return name
        return self.model.default_profile

    def _tuning_blocked(self, conditions):
        """Why manual tuning is not allowed now, or an empty string."""
        if self.profile == self.model.default_profile:
            return f"Manual tuning is not available in {self.model.default_profile}."
        missing = self._missing(self.model.tuning_requires, conditions)
        return f"Changing limits requires {describe_missing(missing)}." if missing else ""

    def _require_tuning(self):
        reason = self._tuning_blocked(self._conditions())
        if reason:
            raise RequestError(reason)

    def evaluate(self):
        """Brings the profile in line with the mode and conditions; cheap when nothing changed."""
        with self.lock:
            conditions = self._conditions()
            if self.cooling_confirmed and not (conditions["ac"] and conditions["lid_open"]):
                self.log.info("cooling confirmation cleared: external power or open lid lost")
                self.cooling_confirmed = conditions["cooling_confirmation"] = False
                self._notify()
            name = self._select(conditions)
            profile = self.model.profiles[name]
            mode_changed = conditions["mode"] != self.mode
            variant_changed = profile.for_source(conditions["source"]) != profile.for_source(self.source)
            if mode_changed:
                self._settle_until = self._now() + ASUSD_SETTLE_S
            if name != self.profile:
                self.log.info("mode %s on %s: %s", conditions["mode"], conditions["source"], name)
            self.mode, self.source = conditions["mode"], conditions["source"]
            if mode_changed or variant_changed or name != self.profile:
                self.profile = name
                self._schedule(APPLY_PROFILE)

    # GPU limits from the driver

    def _gpu_limits(self):
        # The limits come from the VBIOS and do not change at runtime. They
        # are cached so that evaluations and battery transitions do not wake
        # a suspended dGPU.
        if self._gpu_info is None and not (
            self.hw.nvidia.suspended() and self.hw.supplies.source() == "battery"
        ):
            try:
                self._gpu_info = self.hw.nvidia.describe()
            except NvidiaError as error:
                self.log.warning("GPU limits unavailable: %s", error)
            else:
                self._persist()
        return self._gpu_info

    def _hardware_max_tgp(self):
        info = self._gpu_limits()
        return None if info is None else int(info["max_power_w"] + 0.001)

    def _clock_ranges(self):
        info = self._gpu_limits()
        if info is None:
            return None
        return {
            "core_offset": Range(*info["core_offset"]),
            "memory_offset": Range(*info["memory_offset"]),
            "limit": Range(*info["clock_limit"]),
        }

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

    # Work

    def _notify(self):
        self.on_change()

    def _schedule(self, level):
        self._pending = max(self._pending, level)
        self._generation += 1
        self._changed.notify_all()
        self._notify()

    def _current(self, generation):
        with self.lock:
            return generation == self._generation

    def _pause(self, seconds, generation):
        """Waits without the lock; False when newer work superseded this one."""
        if seconds > 0:
            self._sleep(seconds)
        return self._current(generation)

    def run_pending(self):
        """Runs pending work in the caller; returns False when there was none."""
        with self.lock:
            if not self._pending:
                return False
            level, self._pending = self._pending, 0
            generation = self._generation
            self._busy = True
        try:
            level = self._run(level, generation)
        except HardwareError as error:
            level = 0
            with self.lock:
                self.last_error = str(error)
                self.log.error(self.last_error)
        with self.lock:
            # Interrupted work continues together with the newer work.
            self._pending = max(self._pending, level)
            self._busy = False
            self._changed.notify_all()
            self._notify()
        return True

    def _run(self, level, generation):
        """Returns the work left over when newer work interrupted it."""
        if level >= APPLY_PROFILE:
            if not self._pause(self._settle_until - self._now(), generation):
                return level
            with self.lock:
                if generation != self._generation:
                    return level
                self._apply_profile()
            self._reset_clocks()
        return 0 if self._sync_gpu(generation) else SYNC_GPU

    def _resolved(self):
        profile = self.model.profiles[self.profile].for_source(self.source)
        override = dict(self.overrides.get(self.profile, {}))
        if "cpu" in override:
            override["cpu"] = tuple(override["cpu"])
        return replace(profile, **override)

    def _apply_profile(self):
        resolved = self._resolved()
        # Profiles never overclock; offsets and the clock limit are opt-in.
        if self.gpu_offsets != (0, 0) or self.gpu_clock_limit:
            self._clock_reset_pending = True
        self.hw.cpu.set_boost(True)
        self.hw.cpu.set_policy(resolved.cpu_epp)
        self.hw.wmi.write_power_budget(resolved)
        ranges = self._clock_ranges()
        self.balance = (
            BatteryBalance(self.model.balance, ranges["limit"].step if ranges else 1, resolved.cpu[0], self._now())
            if resolved.battery_balance
            else None
        )
        self.last_error = ""
        self.log.info("applied %s (%s mode, %s)", self.profile, self.mode, self.source)

    def _write_nvpcf(self, profile):
        info = self._gpu_limits()
        base = self.hw.wmi.base_tgp()
        if info is None or base is None:
            self.log.warning("NVPCF override not written: GPU limits or base TGP unknown")
            return None
        self.nvpcf = nvpcf_policy(info["default_power_w"], info["max_power_w"], base, profile)
        write_nvpcf_override(self.nvpcf_file, self.nvpcf)
        return self.nvpcf["base_tgp_w"]

    def _sync_gpu(self, generation):
        """Updates the NVPCF override and lets nvidia-powerd pick it up."""
        with self.lock:
            if generation != self._generation:
                return False
            if self.holding:
                return True
            expected = self._write_nvpcf(self._resolved())
            on_battery = self.source == "battery"
        if expected is None or on_battery:
            # On battery the firmware caps the dGPU (40-45 W on the PX13) and
            # disables Dynamic Boost, and asusd may stop nvidia-powerd. The
            # override is ready for its next start; restarting it now would
            # wake the dGPU to verify an unreachable ceiling.
            return True
        # nvidia-powerd reads the firmware TGP/Dynamic Boost budget when it
        # starts; without a restart it can keep the previous ceiling.
        detail = "power ceiling did not update"
        for attempt in range(SYNC_ATTEMPTS):
            # Let the GPU WMI transaction settle; reasserting TGP here starts a
            # new firmware negotiation and powerd observes the old limit.
            if not self._pause(1, generation):
                return False
            with self.lock:
                if generation != self._generation:
                    return False
                # Dynamic Boost also evaluates the CPU budget at startup.
                # Repeating only PPT right before powerd matches the stable
                # firmware sequence used by Armoury Crate and G-Helper.
                self.hw.wmi.write_cpu_power(self._resolved().cpu)
            if not self._pause(1, generation):
                return False
            ok, detail = self.hw.nvidia.restart_powerd()
            if not ok:
                continue
            if not self._pause(2, generation):
                return False
            try:
                ceiling = self.hw.nvidia.ceiling_w()
            except NvidiaError:
                ceiling = None
            # NVIDIA reports some mobile limits one watt below the firmware
            # value, for example 79 W for an 80 W ceiling.
            if ceiling is None or ceiling >= expected - 2.0:
                self.log.info("nvidia-powerd synchronized: ceiling %s W, maximum %d W",
                              "unknown" if ceiling is None else f"{ceiling:.0f}", self.nvpcf["max_tgp_w"])
                return True
            detail = f"ceiling {ceiling:.0f} W, expected {expected} W"
            self.log.warning("nvidia-powerd synchronization attempt %d failed: %s", attempt + 1, detail)
        raise HardwareError(f"nvidia-powerd synchronization failed: {detail}")

    def wait_idle(self, timeout=IDLE_TIMEOUT_S):
        """Waits until all work is applied. Without a worker thread it runs here."""
        if self._worker is None:
            while self.run_pending():
                pass
            return
        deadline = time.monotonic() + timeout
        with self.lock:
            while (self._pending or self._busy) and time.monotonic() < deadline:
                self._changed.wait(deadline - time.monotonic())

    # GPU clocks

    def _set_clocks(self, offsets=None, limit=None):
        """Applies offsets and/or the clock limit through NVML; not under the state lock."""
        with self._clock_lock:
            with self.lock:
                ranges = self._clock_ranges()
            if offsets is not None:
                self.hw.nvidia.set_offsets(*offsets)
            if limit is not None:
                self.hw.nvidia.set_clock_limit(ranges["limit"].min if ranges else 0, limit)
            with self.lock:
                if offsets is not None:
                    self.gpu_offsets = offsets
                if limit is not None:
                    self.gpu_clock_limit = limit

    def _reset_clocks(self):
        """Restores the default clocks when a reset is pending; not under the state lock."""
        with self.lock:
            # A suspended dGPU runs no clocks; the guard resets them once
            # something else has woken it.
            if not self._clock_reset_pending or self.hw.nvidia.suspended():
                return
            self._clock_reset_time = self._now()
        try:
            self._set_clocks((0, 0), 0)
        except NvidiaError as error:
            self.log.warning("GPU clock reset failed: %s", error)
            return
        with self.lock:
            self._clock_reset_pending = False

    # Guard, suspend and shutdown

    def _balance_step(self, now):
        """Applies the CPU target; returns a GPU clock limit to apply, or None."""
        targets = self.balance.update(now, self.monitor)
        if targets is None:
            return None
        spl, clock = targets
        self._notify()
        try:
            if spl != self.balance.spl:
                self.hw.wmi.write_cpu_power(BatteryBalance.cpu_power(spl))
                self.balance.spl = spl
        except HardwareError as error:
            self.last_error = f"battery balance failed: {error}"
            self.log.error(self.last_error)
        return clock if self.balance.wants_clock(clock) and self._clock_ranges() else None

    def _watch_battery(self, now):
        """Measures the battery on external power; returns a balance clock limit or None.

        Warns when the battery drains although nothing can lower the load
        further, and arms the rescue when that happens at a critical level.
        """
        battery = None if self.source == "battery" else self.hw.supplies.battery()
        if battery is None:
            self.monitor.forget()
            self._weak_since = None
            return None
        self.monitor.update(now, battery)
        clock = self._balance_step(now) if self.balance is not None else None
        weak = self.monitor.draining and (self.balance is None or self.balance.at_minimum)
        warning = CHARGER_TOO_WEAK + (USB_C_HINT if self.source == "usb-c" else "") if weak else ""
        if (weak or self.last_error.startswith(CHARGER_TOO_WEAK)) and warning != self.last_error:
            self.last_error = warning
            if weak:
                self.log.warning(warning)
            self._notify()
        if not battery.critical:
            self._rescued = False
        self._weak_since = (self._weak_since or now) if weak and battery.critical else None
        return clock

    def _rescue_due(self, now):
        if self._rescued or self._weak_since is None or now - self._weak_since < RESCUE_DELAY_S:
            return False
        self._rescued = True
        return True

    def _rescue(self):
        """Puts the system to sleep, where even a weak input recharges the battery."""
        try:
            action = self.hw.sleep_system()
        except HardwareError as error:
            self.log.error("critical battery rescue failed: %s", error)
        else:
            self.log.warning("battery critical and draining on external power: %s", action)

    def guard_tick(self):
        """Runs once a second: follows the mode and conditions, watches the battery."""
        self.evaluate()
        with self.lock:
            if self.holding or self._pending or self._busy:
                return
            now = self._now()
            clock = self._watch_battery(now)
            rescue = self._rescue_due(now)
            balance = self.balance
            reset_due = self._clock_reset_pending and (
                self._clock_reset_time is None or now - self._clock_reset_time >= CLOCK_RESET_RETRY_S
            )
        if clock is not None:
            try:
                self._set_clocks(limit=clock)
            except NvidiaError as error:
                self.log.warning("battery balance clock limit failed: %s", error)
            else:
                balance.gpu_clock = clock
        if reset_due:
            self._reset_clocks()
        if rescue:
            self._rescue()

    def _hold_default(self, reason):
        with self.lock:
            self.log.info("holding %s: %s", self.model.default_profile, reason)
            self.holding = True
            self.cooling_confirmed = False
            self.evaluate()
        self.wait_idle()

    def prepare_suspend(self, source):
        with self.lock:
            handled = self.holding
        if handled:
            self.log.info("suspend already handled; source=%s", source)
        else:
            self._hold_default(f"system is suspending ({source})")
        return self.state()

    def resume(self, source):
        with self.lock:
            handled = self._resume_started
            if not handled:
                self._resume_started = True
                # Keep the default profile until the NVIDIA driver is back.
                self.holding = True
                self.cooling_confirmed = False
                self.evaluate()
        if handled:
            self.log.info("resume already handled; source=%s", source)
        else:
            threading.Thread(target=self._resume_worker, daemon=True).start()
        return self.state()

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
            self.holding = False
            self._resume_started = False
            self.cooling_confirmed = False
            if ready != 3:
                self.last_error = "GPU was not ready after resume"
                self.log.error(self.last_error)
            self.evaluate()

    def shutdown(self):
        """Leaves the hardware in the default profile when the service stops."""
        self._hold_default("service is stopping")

    def start(self):
        with self.lock:
            self.hw.cpu.ensure_active_pstate(self.log)
            # nvidia-powerd may start before the first profile is applied.
            self._write_nvpcf(self.model.profiles[self.model.default_profile])
            self.evaluate()

        def worker():
            while True:
                with self.lock:
                    while not self._pending:
                        self._changed.wait()
                self.run_pending()

        def guard():
            while True:
                self._sleep(1)
                self.guard_tick()

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()
        threading.Thread(target=guard, daemon=True).start()

    # Requests

    def reset(self):
        """Drops manual changes of the active profile and applies it again."""
        with self.lock:
            self.overrides.pop(self.profile, None)
            self._persist()
            self._schedule(APPLY_PROFILE)
        return self.state()

    def confirm_cooling(self):
        with self.lock:
            conditions = self._conditions()
            if not (conditions["ac"] and conditions["lid_open"]):
                raise RequestError("Cooling can only be confirmed on external power with the lid open")
            self.cooling_confirmed = True
            self.evaluate()
            self._notify()
        return self.state()

    def set_cpu_power(self, sustained, slow, fast):
        values = (int(sustained), int(slow), int(fast))
        with self.lock:
            if not self.model.cpu.accepts(values):
                raise RequestError(f"CPU power must fit {self.model.cpu.describe()}")
            self._require_tuning()
            previous = self.hw.wmi.cpu_power()
            try:
                self.hw.wmi.write_cpu_power(values)
            except HardwareError as error:
                if None not in previous:
                    try:
                        self.hw.wmi.write_cpu_power(previous)
                    except HardwareError:
                        self.log.exception("CPU power rollback failed")
                self.last_error = str(error)
                self._notify()
                raise
            if self.balance is not None:
                self.balance.set_ceiling(values[0])
            self.overrides.setdefault(self.profile, {})["cpu"] = list(values)
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
            self.hw.wmi.write_power_budget(replace(self._resolved(), **values))
            self.overrides.setdefault(self.profile, {}).update(values)
            self._persist()
            self.last_error = ""
            self._schedule(SYNC_GPU)
        return self.state()

    def _clock_request(self, offsets=None, limit=None):
        with self.lock:
            self._require_tuning()
            if self.balance is not None:
                raise RequestError("The battery balance controls the GPU clocks in this profile")
        try:
            self._set_clocks(offsets, limit)
        except NvidiaError as error:
            with self.lock:
                self.last_error = str(error)
                self._notify()
            raise
        with self.lock:
            self._clock_reset_pending = False
            self.last_error = ""
            self._notify()
        return self.state()

    def set_gpu_offsets(self, core, memory):
        core, memory = int(core), int(memory)
        ranges = self._clock_ranges()
        if ranges is None:
            raise RequestError("GPU clock ranges are unknown until the GPU is awake")
        if core not in ranges["core_offset"] or memory not in ranges["memory_offset"]:
            raise RequestError(
                f"GPU offsets must fit core {ranges['core_offset'].describe('MHz')} "
                f"and memory {ranges['memory_offset'].describe('MHz')}"
            )
        return self._clock_request(offsets=(core, memory))

    def set_gpu_clock_limit(self, limit):
        limit = int(limit)
        ranges = self._clock_ranges()
        if ranges is None:
            raise RequestError("GPU clock ranges are unknown until the GPU is awake")
        if limit != 0 and limit not in ranges["limit"]:
            raise RequestError(f"GPU clock limit must be 0 (default) or {ranges['limit'].describe('MHz')}")
        return self._clock_request(limit=limit)

    # Reports

    def capabilities(self):
        with self.lock:
            model, wmi = self.model, self.hw.wmi
            caps = {
                "name": model.name,
                "default_profile": model.default_profile,
                "modes": {mode: list(names) for mode, names in model.modes.items()},
                "requires_cooling_confirmation": model.requires_cooling_confirmation,
            }
            if wmi.has_cpu_power():
                caps["cpu_power"] = {key: getattr(model.cpu, key).to_json() for key in ("sustained", "slow", "fast")}
            base_tgp, hardware_max = wmi.base_tgp(), self._hardware_max_tgp()
            if base_tgp and hardware_max and hardware_max >= base_tgp + model.gpu.dynamic_boost.min:
                ranges = self._gpu_power_ranges(base_tgp, hardware_max)
                caps["gpu_power"] = {key: value.to_json() for key, value in ranges.items()}
                caps["gpu_power"]["max_total"] = hardware_max
            ranges = self._clock_ranges()
            if ranges is not None:
                caps["gpu_clocks"] = {key: value.to_json() for key, value in ranges.items()}
            return caps

    def state(self):
        telemetry = {
            **self.hw.nvidia.telemetry(),
            "cpu_temperature_c": self.hw.cpu.temperature_c(),
            "cpu_frequency_mhz": self.hw.cpu.frequency_mhz(),
        }
        with self.lock:
            conditions = self._conditions()
            blocked = self._tuning_blocked(conditions)
            wmi = self.hw.wmi.values()
            state = {
                "mode": self.mode,
                "active_profile": self.profile,
                "applying": bool(self._pending or self._busy),
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
                "telemetry": telemetry,
                "last_error": self.last_error,
                # Diagnostics for px13ctl.
                "power_source": conditions["source"],
                "lid_open": conditions["lid_open"],
                "cpu_boost": self.hw.cpu.boost_active(),
                "holding_default": self.holding,
                "battery": self.monitor.to_json(),
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
        for name, override in data.get("overrides", {}).items():
            if name in self.model.profiles and name != self.model.default_profile and self._valid_override(override):
                self.overrides[name] = override
        info = data.get("gpu_info")
        if isinstance(info, dict) and {"default_power_w", "max_power_w", "core_offset", "memory_offset",
                                       "clock_limit"} <= set(info):
            self._gpu_info = info

    def _persist(self):
        try:
            self.state_file.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            temporary = self.state_file.with_suffix(".tmp")
            temporary.write_text(
                json.dumps({"overrides": self.overrides, "gpu_info": self._gpu_info}, sort_keys=True),
                encoding="ascii",
            )
            temporary.replace(self.state_file)
        except OSError as error:
            self.log.warning("state file write failed: %s", error)
