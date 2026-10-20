# SPDX-License-Identifier: MIT
"""NVIDIA driver interfaces: NVML and nvidia-powerd.

Any NVML client resumes a runtime-suspended dGPU from D3cold and keeps it
awake for tens of seconds, so callers check suspended() first where waking it
is not needed. NVML is opened for one operation at a time for the same reason.
"""

import time
from contextlib import contextmanager
from pathlib import Path

import pynvml

from .hardware import HardwareError, read_text, run

PCI_DEVICES = Path("/sys/bus/pci/devices")
NVIDIA_VENDOR = "0x10de"


class NvidiaError(HardwareError):
    pass


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
    def __init__(self):
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

    @contextmanager
    def _gpu(self):
        try:
            pynvml.nvmlInit()
        except pynvml.NVMLError as error:
            raise NvidiaError(f"NVML unavailable: {error}") from error
        try:
            yield pynvml.nvmlDeviceGetHandleByIndex(0)
        except pynvml.NVMLError as error:
            raise NvidiaError(f"NVML: {error}") from error
        finally:
            pynvml.nvmlShutdown()

    def responds(self):
        try:
            with self._gpu():
                return True
        except NvidiaError:
            return False

    def describe(self):
        """Static GPU limits: VBIOS power limits in W and clock ranges in MHz."""
        with self._gpu() as gpu:
            memory_clock = pynvml.nvmlDeviceGetSupportedMemoryClocks(gpu)[0]
            clocks = sorted(pynvml.nvmlDeviceGetSupportedGraphicsClocks(gpu, memory_clock))
            return {
                "default_power_w": pynvml.nvmlDeviceGetPowerManagementDefaultLimit(gpu) / 1000,
                "max_power_w": pynvml.nvmlDeviceGetPowerManagementLimitConstraints(gpu)[1] / 1000,
                "core_offset": list(pynvml.nvmlDeviceGetGpcClkMinMaxVfOffset(gpu)),
                "memory_offset": list(pynvml.nvmlDeviceGetMemClkMinMaxVfOffset(gpu)),
                "clock_limit": [clocks[0], clocks[-1], clocks[1] - clocks[0]],
            }

    def ceiling_w(self):
        """The power limit the driver currently enforces."""
        with self._gpu() as gpu:
            return pynvml.nvmlDeviceGetEnforcedPowerLimit(gpu) / 1000

    def telemetry(self):
        now = time.monotonic()
        if now - self._telemetry_time < 1.0:
            return self._telemetry
        status = self.runtime_status()
        power = temperature = clock = None
        if status != "suspended":
            try:
                with self._gpu() as gpu:
                    power = pynvml.nvmlDeviceGetPowerUsage(gpu) / 1000
                    temperature = pynvml.nvmlDeviceGetTemperature(gpu, pynvml.NVML_TEMPERATURE_GPU)
                    clock = pynvml.nvmlDeviceGetClockInfo(gpu, pynvml.NVML_CLOCK_GRAPHICS)
            except NvidiaError:
                pass
        self._telemetry = {
            "gpu_power_w": power,
            "gpu_temperature_c": temperature,
            "gpu_clock_mhz": clock,
            "gpu_runtime_status": status,
        }
        self._telemetry_time = now
        return self._telemetry

    def set_offsets(self, core, memory):
        with self._gpu() as gpu:
            pynvml.nvmlDeviceSetGpcClkVfOffset(gpu, core)
            pynvml.nvmlDeviceSetMemClkVfOffset(gpu, memory)

    def set_clock_limit(self, minimum, limit):
        """Caps the graphics clock at limit MHz; 0 restores the driver default."""
        with self._gpu() as gpu:
            if limit:
                pynvml.nvmlDeviceSetGpuLockedClocks(gpu, minimum, limit)
            else:
                pynvml.nvmlDeviceResetGpuLockedClocks(gpu)

    def restart_powerd(self):
        run(["/usr/bin/systemctl", "reset-failed", "nvidia-powerd.service"], timeout=10)
        return run(["/usr/bin/systemctl", "restart", "nvidia-powerd.service"], timeout=10)
