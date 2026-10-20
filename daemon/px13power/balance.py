# SPDX-License-Identifier: MIT
"""Battery balance controller for gaming on a weak charger.

On external power a PI controller keeps the battery gently charging. One
0..100 % power level drives the CPU sustained limit first and the GPU clock
ceiling only below half, because GPU clocks matter most for frame pacing. The
response is deliberately slow so short spikes are absorbed by the battery
instead of cutting clocks.

The battery flow comes from BatteryMonitor. While the gauge reads empty only
the direction is known; a draining battery then holds the lowest level until
the gauge recovers.
"""

PERIOD_S = 5
SETPOINT_W = 4.0
KP = 1.0  # % per W of change in the averaged error
KI = 0.02  # % per W*s of averaged error
MAX_STEP = 3.0  # % per period
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
    def at_minimum(self):
        return self.level == 0.0

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

    def update(self, now, monitor):
        """Every period returns the (spl, clock) targets for the measured flow.

        Returns None between periods and while there is no reading. A full or
        holding battery shows no headroom, so the level is kept.
        """
        elapsed = now - self._last_eval
        if elapsed < PERIOD_S:
            return None
        self._last_eval = now
        if monitor.net_power_w is None:
            self._last_error = None
            if monitor.draining:
                self.level = 0.0
                return self.outputs(self.level)
            return None
        error = monitor.net_power_w - SETPOINT_W
        derivative = 0.0 if self._last_error is None else error - self._last_error
        self._last_error = error
        change = KP * derivative + KI * error * elapsed
        self.level = min(100.0, max(0.0, self.level + max(-MAX_STEP, min(MAX_STEP, change))))
        return self.outputs(self.level)

    def to_json(self):
        return {
            "level_percent": round(self.level, 1),
            "cpu_spl_w": self.spl,
            "gpu_clock_limit_mhz": self.gpu_clock,
        }
