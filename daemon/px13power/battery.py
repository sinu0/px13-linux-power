# SPDX-License-Identifier: MIT
"""Battery flow measurement on external power.

The PX13 firmware keeps reporting "Charging" with a positive power_now while
the battery actually discharges on an insufficient input, so neither status
nor power_now can provide the sign. The flow is derived from the change of
the stored energy over a window instead. A critical battery reads 0 Wh; then
only the trend of its voltage shows whether it still discharges.
"""

WINDOW_S = 30.0
MIN_WINDOW_S = 15.0
# energy_now has whole-µWh steps, so a 30 s window reads in steps of about
# 1.9 W; smaller losses cannot be told from noise.
DRAIN_W = 2.0
# A voltage falling faster than this means discharging: 15 mV in 30 s. Measured
# discharges on the PX13 fell 2 to 10 mV/s, charging rose 0.7 to 14 mV/s.
VOLTAGE_DRAIN_V_PER_S = 0.0005


class BatteryMonitor:
    def __init__(self):
        self.critical = False
        self.forget()

    def forget(self):
        """Drops the window, e.g. while running from the battery."""
        self._samples = []  # (time, Wh, V)
        self.net_power_w = None  # positive while charging
        self.draining = False

    def update(self, now, battery):
        if battery.critical != self.critical:
            # Energy readings on both sides of the change are not comparable.
            self.critical = battery.critical
            self.forget()
        self._samples.append((now, battery.energy_wh, battery.voltage_v))
        while len(self._samples) > 2 and now - self._samples[1][0] >= WINDOW_S:
            self._samples.pop(0)
        if battery.full:
            self.net_power_w, self.draining = None, False
        elif battery.critical:
            change = self._change(2)
            self.net_power_w = None
            self.draining = change is not None and change < -VOLTAGE_DRAIN_V_PER_S
        else:
            change = self._change(1)
            self.net_power_w = None if change is None else change * 3600.0
            self.draining = self.net_power_w is not None and self.net_power_w < -DRAIN_W

    def _change(self, index):
        """Change of a sample field per second over the window, if long enough."""
        if len(self._samples) < 2:
            return None
        first, last = self._samples[0], self._samples[-1]
        if last[0] - first[0] < MIN_WINDOW_S or first[index] is None or last[index] is None:
            return None
        return (last[index] - first[index]) / (last[0] - first[0])

    def to_json(self):
        return {
            "net_w": None if self.net_power_w is None else round(self.net_power_w, 1),
            "draining": self.draining,
            "critical": self.critical,
        }
