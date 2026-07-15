# Security and safety policy

## Supported hardware

Only `ASUSTeK COMPUTER INC.` / board `HN7306WV` is supported. Reports for other
models must include independently captured WMI ranges, NVIDIA limits and NVPCF
behavior. Do not weaken the DMI gates to test another model.

## Reporting

When reporting a safety or security problem, include the project version,
kernel, NVIDIA driver, BIOS version, `scripts/doctor.sh` output and relevant
service logs. Remove serial numbers and other machine identifiers.

## Safety boundaries

- The project never installs a VBIOS.
- It never raises the NVIDIA-reported firmware maximum.
- High power requires AC, open lid and explicit cooling confirmation.
- Startup, suspend/resume and failed guards return to Safe.
- Default GPU offsets are zero.
- Malformed, writable or non-root NVPCF policy files are ignored.

The daemon retains only two capabilities: `CAP_SYS_ADMIN`, required by NVIDIA
NVML clock lock/reset, and `CAP_DAC_OVERRIDE`, required to traverse the user's
mode-0700 `/run/user/<uid>` directory for the Xwayland authority file. Home
directories are hidden, `/run/user` is bind-mounted read-only, the system
filesystem is read-only, and only the exact WMI/CPU sysfs paths are writable.

Driver, firmware and kernel updates can invalidate assumptions. After such an
update, remain in Safe until `scripts/doctor.sh` passes and logs show the exact
NVPCF override line.
