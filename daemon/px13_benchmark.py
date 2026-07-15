#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Record a controlled PX13 GPU/CPU benchmark and always return to Safe."""

import argparse
import csv
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


RESULTS_DIR = Path.home() / ".local/state/px13-powerd/tests"
HWMON_PATH = Path("/sys/class/hwmon")
MODULE_PARAMETERS = Path("/sys/module/px13_tgp_probe/parameters")
GPU_TEMPERATURE_GUARD_C = 88.0


def controller(command):
    result = subprocess.run(
        ["/usr/local/bin/px13ctl", command],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def set_module_parameter(name, value):
    result = subprocess.run(
        ["/usr/bin/sudo", "/usr/bin/tee", str(MODULE_PARAMETERS / name)],
        input=f"{value}\n",
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"unable to set {name}: {detail}")


def cpu_temperature():
    for name_path in HWMON_PATH.glob("*/name"):
        try:
            if name_path.read_text(encoding="ascii").strip() != "k10temp":
                continue
            return round(
                int((name_path.parent / "temp1_input").read_text(encoding="ascii")) / 1000.0,
                1,
            )
        except (OSError, ValueError):
            return None
    return None


def cpu_frequency():
    values = []
    for policy in Path("/sys/devices/system/cpu/cpufreq").glob("policy*"):
        try:
            values.append(int((policy / "scaling_cur_freq").read_text(encoding="ascii")))
        except (OSError, ValueError):
            continue
    if not values:
        return None
    return round(sum(values) / (1000.0 * len(values)))


def fan_speeds():
    speeds = {"cpu_fan_rpm": None, "gpu_fan_rpm": None}
    for name_path in HWMON_PATH.glob("*/name"):
        try:
            if name_path.read_text(encoding="ascii").strip() != "asus":
                continue
            for index in (1, 2):
                label = (name_path.parent / f"fan{index}_label").read_text(
                    encoding="ascii"
                ).strip()
                value = int(
                    (name_path.parent / f"fan{index}_input").read_text(encoding="ascii")
                )
                if label in ("cpu_fan", "gpu_fan"):
                    speeds[f"{label}_rpm"] = value
        except (OSError, ValueError):
            continue
    return speeds


def gpu_telemetry():
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
        return (None, None, None)
    if result.returncode != 0 or not result.stdout.strip():
        return (None, None, None)
    values = result.stdout.splitlines()[0].split(",")
    try:
        return tuple(float(value.strip()) for value in values)
    except (ValueError, TypeError):
        return (None, None, None)


def start_process(command, environment, log_path):
    output = log_path.open("w", encoding="ascii")
    process = subprocess.Popen(
        command,
        env=environment,
        start_new_session=True,
        stdout=output,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return process, output


def stop_process(process):
    if process is None or process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def main():
    parser = argparse.ArgumentParser(
        description="Run a measured PX13 benchmark; the controller returns to Safe when it ends."
    )
    parser.add_argument("--profile", choices=("gaming", "120w"), default="gaming")
    parser.add_argument("--duration", type=int, default=300)
    parser.add_argument("--cpu-load", action="store_true", help="add stress-ng CPU load")
    parser.add_argument(
        "--settle-seconds",
        type=int,
        default=0,
        help="wait after profile activation before starting the workload",
    )
    parser.add_argument(
        "--gpu-focus-cpu",
        action="store_true",
        help="temporarily use the minimum 5 W CPU PPT limits to test GPU power sharing",
    )
    parser.add_argument(
        "--benchmark",
        choices=("furmark", "glmark2", "vkmark", "none"),
        default="glmark2",
        help="furmark is the Vulkan sustained-load test; all GPU tests use NVIDIA offload",
    )
    args = parser.parse_args()
    if args.duration < 15:
        parser.error("--duration must be at least 15 seconds")
    if args.settle_seconds < 0 or args.settle_seconds > 60:
        parser.error("--settle-seconds must be between 0 and 60")

    requested_command = "120w" if args.profile == "120w" else "gaming"
    before = controller("state")
    if args.profile == "120w" and not before["cooling_confirmed"]:
        parser.error(
            "120W requires a fresh physical cooling confirmation in RCC or `px13ctl confirm-cooling`"
        )

    state = controller(requested_command)
    if state["active_profile"] != ("120W" if args.profile == "120w" else "Gaming"):
        raise RuntimeError("controller did not activate the requested profile")
    if args.gpu_focus_cpu:
        for parameter in ("cpu_spl", "cpu_sppt", "cpu_fppt"):
            set_module_parameter(parameter, 5)
        state = controller("state")
    if args.settle_seconds:
        time.sleep(args.settle_seconds)

    RESULTS_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    csv_path = RESULTS_DIR / f"{stamp}-{args.profile}.csv"
    metadata_path = RESULTS_DIR / f"{stamp}-{args.profile}.json"
    benchmark_log_path = RESULTS_DIR / f"{stamp}-{args.profile}.benchmark.log"
    cpu_log_path = RESULTS_DIR / f"{stamp}-{args.profile}.cpu.log"
    metadata = {
        "profile": state,
        "duration_seconds": args.duration,
        "cpu_load": args.cpu_load,
        "gpu_focus_cpu": args.gpu_focus_cpu,
        "benchmark": args.benchmark,
        "benchmark_log": str(benchmark_log_path),
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="ascii")

    environment = os.environ.copy()
    environment["__NV_PRIME_RENDER_OFFLOAD"] = "1"
    environment["__VK_LAYER_NV_optimus"] = "NVIDIA_only"
    environment["__GLX_VENDOR_LIBRARY_NAME"] = "nvidia"
    benchmark = None
    cpu_load = None
    benchmark_output = None
    cpu_output = None
    try:
        if args.benchmark == "glmark2":
            benchmark, benchmark_output = start_process(
                [
                    "/usr/bin/glmark2-wayland",
                    "--off-screen",
                    "--size",
                    "1920x1080",
                    "--frame-end",
                    "finish",
                    "--benchmark",
                    "loop:fragment-steps=1000:duration=10",
                    "--run-forever",
                ],
                environment,
                benchmark_log_path,
            )
        elif args.benchmark == "vkmark":
            benchmark, benchmark_output = start_process(
                [
                    "/usr/bin/vkmark",
                    "--winsys",
                    "wayland",
                    "--size",
                    "3840x2160",
                    "--present-mode",
                    "mailbox",
                    "--benchmark",
                    "shading:shading=blinn-phong-inf:duration=10",
                    "--run-forever",
                ],
                environment,
                benchmark_log_path,
            )
        elif args.benchmark == "furmark":
            # Keep FurMark windowed so it cannot interfere with the active session.
            # The parent terminates it exactly at --duration and always restores Safe.
            benchmark, benchmark_output = start_process(
                [
                    "/usr/bin/furmark",
                    "--demo",
                    "furmark-vk",
                    "--hpgfx",
                    "1",
                    "--width",
                    "1920",
                    "--height",
                    "1080",
                    "--title-bar",
                    "0",
                    "--no-resize",
                    "--no-osi",
                    "--vsync",
                    "0",
                    "--max-time",
                    str(args.duration + 30),
                ],
                environment,
                benchmark_log_path,
            )
        if args.cpu_load:
            cpu_load, cpu_output = start_process(
                [
                    "/usr/bin/stress-ng",
                    "--cpu",
                    "0",
                    "--cpu-method",
                    "matrixprod",
                    "--timeout",
                    str(args.duration),
                    "--metrics-brief",
                ],
                environment,
                cpu_log_path,
            )

        started = time.monotonic()
        peak_gpu_power = 0.0
        with csv_path.open("w", encoding="ascii", newline="") as output:
            writer = csv.DictWriter(
                output,
                fieldnames=(
                    "elapsed_s",
                    "gpu_power_w",
                    "gpu_temperature_c",
                    "gpu_clock_mhz",
                    "cpu_temperature_c",
                    "cpu_frequency_mhz",
                    "cpu_fan_rpm",
                    "gpu_fan_rpm",
                    "profile",
                ),
            )
            writer.writeheader()
            while time.monotonic() - started < args.duration:
                if benchmark is not None and benchmark.poll() is not None:
                    raise RuntimeError(
                        f"benchmark exited with code {benchmark.returncode}; see {benchmark_log_path}"
                    )
                gpu_power, gpu_temperature, gpu_clock = gpu_telemetry()
                fans = fan_speeds()
                if gpu_power is not None:
                    peak_gpu_power = max(peak_gpu_power, gpu_power)
                if (
                    gpu_temperature is not None
                    and gpu_temperature >= GPU_TEMPERATURE_GUARD_C
                ):
                    raise RuntimeError(
                        f"GPU reached {gpu_temperature:.1f} C; stopped at the {GPU_TEMPERATURE_GUARD_C:.0f} C safety guard"
                    )
                writer.writerow(
                    {
                        "elapsed_s": round(time.monotonic() - started, 1),
                        "gpu_power_w": gpu_power,
                        "gpu_temperature_c": gpu_temperature,
                        "gpu_clock_mhz": gpu_clock,
                        "cpu_temperature_c": cpu_temperature(),
                        "cpu_frequency_mhz": cpu_frequency(),
                        "cpu_fan_rpm": fans["cpu_fan_rpm"],
                        "gpu_fan_rpm": fans["gpu_fan_rpm"],
                        "profile": args.profile,
                    }
                )
                output.flush()
                time.sleep(1)
        if args.benchmark != "none" and peak_gpu_power < 20.0:
            raise RuntimeError(
                f"GPU load was only {peak_gpu_power:.1f} W; benchmark result is invalid, see {benchmark_log_path}"
            )
    finally:
        stop_process(benchmark)
        stop_process(cpu_load)
        if benchmark_output is not None:
            benchmark_output.close()
        if cpu_output is not None:
            cpu_output.close()
        try:
            controller("safe")
        except subprocess.CalledProcessError as error:
            print(f"px13-benchmark: unable to restore Safe: {error}", file=sys.stderr)

    print(csv_path)
    print(metadata_path)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(f"px13-benchmark: {error}", file=sys.stderr)
        sys.exit(1)
