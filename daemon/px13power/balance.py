# SPDX-License-Identifier: MIT
"""Battery balance controller for gaming on a weak charger.

On external power a PI controller keeps the battery gently charging. One
0..100 % power level drives the CPU sustained limit first and the GPU clock
ceiling only below half, because GPU clocks matter most for frame pacing. The
response is deliberately slow so short spikes are absorbed by the battery
instead of cutting clocks.

The battery flow is derived from the change of the stored energy over a
window: the PX13 firmware keeps reporting "Charging" with a positive
power_now while the battery actually discharges under a USB-C load, so status
and power_now cannot provide the sign. A critical battery reads 0 Wh, so
there only the trend of its voltage tells whether it still discharges; then
the lowest level is held until the gauge recovers.
"""

PERIOD_S = 5
SETPOINT_W = 4.0
WINDOW_S = 30.0
MIN_WINDOW_S = 15.0
KP = 1.0  # % per W of change in the averaged error
KI = 0.02  # % per W*s of averaged error
MAX_STEP = 3.0  # % per period
# A voltage falling faster than this means discharging: 15 mV in 30 s. Measured
# discharges on the PX13 fell 2 to 10 mV/s, charging rose 0.7 to 14 mV/s.
VOLTAGE_DRAIN_V_PER_S = 0.0005
CLOCK_HYSTERESIS_STEPS = 3


def _share(value):
    return min(1.0, max(0.0, value))


class BatteryBalance:
    def __init__(self, limits, clock_step, spl, now):
        self.limits = limits
        self.clock_step = clock_step
        self.spl = spl
        self.spl_ceiling = limits.cpu_spl.max
        # Start where the profile's own sustained limit is, GPU unlimited.
        spl_range = self.spl_ceiling - limits.cpu_spl.min
        self.level = 50.0 + 50.0 * _share((spl - limits.cpu_spl.min) / spl_range if spl_range else 0.0)
        self.gpu_clock = 0  # 0 = no limit
        self.net_power_w = None
        self.draining = False
        self._samples = []  # (time, Wh, V)
        self._critical = False
        self._last_error = None
        self._last_eval = now

    @staticmethod
    def cpu_power(spl):
        """CPU limits for a sustained limit, keeping the usual burst headroom."""
        return spl, spl + 5, spl + 10

    def set_ceiling(self, spl):
        """A manual CPU change becomes the new sustained limit ceiling."""
        self.spl = spl
        self.spl_ceiling = max(self.limits.cpu_spl.min, spl)

    @property
    def charger_too_weak(self):
        """The battery drains even at the lowest level."""
        return self.draining and self.level == 0.0

    def forget(self):
        """Drops the measurement window, e.g. while running from the battery."""
        self._samples = []
        self.net_power_w = None
        self.draining = False

    def outputs(self, level):
        """Maps a power level to (CPU sustained W, GPU clock limit MHz or 0)."""
        spl_min = self.limits.cpu_spl.min
        spl = round(spl_min + (self.spl_ceiling - spl_min) * _share((level - 50.0) / 50.0))
        gpu_share = _share(level / 50.0)
        if gpu_share >= 1.0:
            return spl, 0
        clocks = self.limits.gpu_clock
        clock = clocks.min + (clocks.max - clocks.min) * gpu_share
        return spl, int(clock // self.clock_step * self.clock_step)

    def wants_clock(self, clock):
        if clock == self.gpu_clock:
            return False
        if clock == 0 or self.gpu_clock == 0:
            return True
        return abs(clock - self.gpu_clock) >= CLOCK_HYSTERESIS_STEPS * self.clock_step

    def update(self, now, battery):
        """Records a battery reading; every period returns the (spl, clock) targets.

        Returns None between periods and while there is no reading. A full or
        holding battery shows no headroom, so the level is kept.
        """
        if battery.critical != self._critical:
            # Energy readings on both sides of the change are not comparable.
            self._critical = battery.critical
            self.forget()
        self._samples.append((now, battery.energy_wh, battery.voltage_v))
        while len(self._samples) > 2 and now - self._samples[1][0] >= WINDOW_S:
            self._samples.pop(0)
        if battery.full:
            self.net_power_w, self.draining = None, False
        elif battery.critical:
            self.net_power_w, self.draining = None, self._voltage_falling()
        else:
            self.net_power_w = self._flow()
            self.draining = self.net_power_w is not None and self.net_power_w < 0
        elapsed = now - self._last_eval
        if elapsed < PERIOD_S:
            return None
        self._last_eval = now
        if self.net_power_w is None:
            self._last_error = None
            if self.draining:
                self.level = 0.0
                return self.outputs(self.level)
            return None
        error = self.net_power_w - SETPOINT_W
        derivative = 0.0 if self._last_error is None else error - self._last_error
        self._last_error = error
        change = KP * derivative + KI * error * elapsed
        self.level = min(100.0, max(0.0, self.level + max(-MAX_STEP, min(MAX_STEP, change))))
        return self.outputs(self.level)

    def _change(self, index):
        """Change of a sample field per second over the window, if long enough."""
        if len(self._samples) < 2:
            return None
        first, last = self._samples[0], self._samples[-1]
        if last[0] - first[0] < MIN_WINDOW_S or first[index] is None or last[index] is None:
            return None
        return (last[index] - first[index]) / (last[0] - first[0])

    def _flow(self):
        """Battery flow in W: positive while charging."""
        change = self._change(1)
        return None if change is None else change * 3600.0

    def _voltage_falling(self):
        change = self._change(2)
        return change is not None and change < -VOLTAGE_DRAIN_V_PER_S

    def to_json(self):
        return {
            "level_percent": round(self.level, 1),
            "cpu_spl_w": self.spl,
            "gpu_clock_limit_mhz": self.gpu_clock,
            "battery_net_w": None if self.net_power_w is None else round(self.net_power_w, 1),
            "battery_draining": self.draining,
        }
