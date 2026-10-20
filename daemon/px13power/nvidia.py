# SPDX-License-Identifier: MIT
"""NVIDIA driver interfaces: nvidia-smi, nvidia-settings and nvidia-powerd.

Any NVML client (nvidia-smi, nvidia-settings) resumes a runtime-suspended
dGPU from D3cold and keeps it awake for tens of seconds, so callers check
suspended() first where waking it is not needed.
"""

import time
from pathlib import Path

from .hardware import read_text, run

SMI = "/usr/bin/nvidia-smi"
PCI_DEVICES = Path("/sys/bus/pci/devices")
NVIDIA_VENDOR = "0x10de"
# Xwayland authority files of a running desktop session: GNOME, then KDE.
XAUTHORITY_PATTERNS = ("*/.mutter-Xwaylandauth.*", "*/xauth_*")


def _number(value):
    value = value.strip()
    if value in {"", "N/A", "[N/A]"}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _query(fields, timeout=5):
    ok, output = run([SMI, f"--query-gpu={fields}", "--format=csv,noheader,nounits"], timeout)
    if not ok or not output.strip():
        return None
    return [_number(value) for value in output.splitlines()[0].split(",")]


def nvpcf_policy(default_limit, max_limit, base_tgp, profile):
    """Computes the NVPCF override for a profile's requested power.

    NVIDIA applies the override on top of its default limit and ignores
    negative cTGP offsets, so a request below the default keeps the default.
    Dynamic Boost is trimmed to the VBIOS maximum.
    """
    requested_base = base_tgp + profile.gpu_tgp_offset
    ctgp_offset = max(0, round(requested_base - default_limit))
    dynamic_boost = profile.gpu_dynamic_boost
    if max_limit is not None:
        dynamic_boost = min(dynamic_boost, max(0, round(max_limit - default_limit - ctgp_offset)))
    return {
        "enabled": True,
        "default_tgp_w": round(default_limit),
        "base_tgp_w": round(default_limit + ctgp_offset),
        "ctgp_offset_w": ctgp_offset,
        "dynamic_boost_w": dynamic_boost,
        "max_tgp_w": round(default_limit + ctgp_offset + dynamic_boost),
    }


def write_nvpcf_override(path, policy):
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        "enabled=1\n"
        f"ctgp_offset_mw={policy['ctgp_offset_w'] * 1000}\n"
        f"max_output_offset_mw={policy['dynamic_boost_w'] * 1000}\n",
        encoding="ascii",
    )
    temporary.chmod(0o600)
    temporary.replace(path)


class Nvidia:
    def __init__(self, log):
        self.log = log
        self._device = None
        self._telemetry = {}
        self._telemetry_time = 0.0

    def _pci_device(self):
        if self._device is None:
            for device in PCI_DEVICES.glob("*"):
                if read_text(device / "vendor") == NVIDIA_VENDOR and read_text(
                    device / "class"
                ).startswith("0x03"):
                    self._device = device
                    break
        return self._device

    def runtime_status(self):
        device = self._pci_device()
        return "unknown" if device is None else read_text(device / "power" / "runtime_status")

    def suspended(self):
        return self.runtime_status() == "suspended"

    def driver_bound(self):
        device = self._pci_device()
        return device is not None and (device / "driver").exists()

    def responds(self):
        ok, output = run([SMI, "--query-gpu=name", "--format=csv,noheader"], timeout=5)
        return ok and bool(output.strip())

    def power_limits(self):
        """Returns the VBIOS (default, maximum) power limits, or (None, None)."""
        values = _query("power.default_limit,power.max_limit")
        if not values or len(values) != 2 or None in values:
            return None, None
        return tuple(values)

    def ceiling_limit(self):
        ok, output = run([SMI, "-q", "-d", "POWER"], timeout=5)
        in_ceiling = False
        for line in output.splitlines() if ok else ():
            if "GPU Ceiling Power Limit" in line:
                in_ceiling = True
            elif in_ceiling and "Current Power Limit" in line:
                return _number(line.split(":", 1)[1].split()[0])
        return None

    def telemetry(self):
        now = time.monotonic()
        if now - self._telemetry_time < 1.0:
            return self._telemetry
        status = self.runtime_status()
        values = None if status == "suspended" else _query(
            "power.draw,temperature.gpu,clocks.current.graphics", timeout=2
        )
        power, temperature, clock = values if values and len(values) == 3 else (None, None, None)
        self._telemetry = {
            "gpu_power_w": power,
            "gpu_temperature_c": temperature,
            "gpu_clock_mhz": clock,
            "gpu_runtime_status": status,
        }
        self._telemetry_time = now
        return self._telemetry

    def set_offsets(self, core, memory):
        """Applies clock offsets through the desktop's Xwayland server."""
        candidates = []
        for pattern in XAUTHORITY_PATTERNS:
            candidates.extend(Path("/run/user").glob(pattern))
        try:
            authority = max(candidates, key=lambda path: path.stat().st_mtime, default=None)
        except OSError as error:
            self.log.info("NVIDIA offsets deferred: %s", error)
            return False
        if authority is None:
            self.log.info("NVIDIA offsets deferred: no Xwayland authority of a desktop session")
            return False
        ok, detail = run(
            [
                "/usr/bin/nvidia-settings",
                "-a",
                f"[gpu:0]/GPUGraphicsClockOffsetAllPerformanceLevels={core}",
                "-a",
                f"[gpu:0]/GPUMemoryTransferRateOffsetAllPerformanceLevels={memory}",
            ],
            timeout=5,
            env={"DISPLAY": ":0", "XAUTHORITY": str(authority)},
        )
        if not ok:
            self.log.warning("NVIDIA offset request %s/%s failed: %s", core, memory, detail)
            return False
        self.log.info("NVIDIA offsets applied: core %+d MHz, memory %+d MHz", core, memory)
        return True

    def set_clock_limit(self, limit):
        command = [SMI, "--reset-gpu-clocks"] if limit == 0 else [SMI, f"--lock-gpu-clocks=0,{limit}"]
        ok, detail = run(command, timeout=5)
        if not ok:
            self.log.warning("NVIDIA clock limit request %s failed: %s", limit, detail)
            return False
        self.log.info("NVIDIA GPU clock limit %s", f"applied: {limit} MHz" if limit else "reset to default")
        return True

    def restart_powerd(self, action):
        run(["/usr/bin/systemctl", "reset-failed", "nvidia-powerd.service"], timeout=10)
        return run(["/usr/bin/systemctl", action, "nvidia-powerd.service"], timeout=10)
