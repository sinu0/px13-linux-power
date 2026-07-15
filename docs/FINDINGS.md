# Technical findings

Test platform: ASUS ProArt PX13 `HN7306WV`, BIOS `316`, NVIDIA RTX 4060 Laptop
GPU and a VBIOS reporting an 80 W default / 120 W maximum.

## Root cause

ASUS WMI exposes the expected controls and accepts the expected values:

- dGPU base TGP: 55 W;
- variable TGP: 0..40 W, producing a requested base of up to 95 W;
- Dynamic Boost: 5..25 W;
- GPU thermal target: 75..87 C;
- AMD SPL/SPPT/FPPT controls.

Those WMI writes alone did not produce the Windows power behavior. Tracing the
successful NVIDIA RM control response consumed by `nvidia-powerd` identified
the dynamic NVPCF command `0x13b`. On this firmware path it returned:

- maximum cTGP offset: 10 W;
- maximum Dynamic Boost output: 10 W;
- target TPP offset: 25 W;
- minimum output offset: -5 W.

Therefore, selecting 95+25 W through ASUS WMI still left NVIDIA userspace with
a lower Linux policy. This explains the observed 92..100 W plateau. Removing
`nvidia-powerd` reduced the result further, so the service was working; it was
working from restricted firmware input.

The correction intercepts only RM-control ioctl `0xc020462a`, command `0x13b`,
dynamic subfunction 3 and protocol versions `0x20..0x25`. It replaces only the
cTGP and maximum-output fields with a root-owned runtime policy. The sum is
limited to 40 W above NVIDIA's 80 W default and is additionally clamped to the
VBIOS maximum by the daemon.

## Measurements

The final sustained FurMark run is recorded locally as
`20260715-232403-120w.csv`:

- peak GPU power: 115.86 W;
- 16 samples at or above 110 W;
- GPU reached its configured 87 C target, after which power settled near
  100 W as expected from thermal regulation;
- maximum measured CPU temperature: 74.9 C;
- fan speed reached approximately 5100/4900 RPM.

Manual testing after fan ramp-up also reached approximately 115 W. The 120 W
number is an allocation ceiling, not a guarantee of continuous draw.

CPU and GPU share a platform budget. Raising CPU sustained power reduced the
Dynamic Boost assigned to the GPU. In one controlled run, CPU 15/25/35 W
limited the GPU peak to 104.64 W. CPU 5/25/35 W allowed 115.86 W while avoiding
the roughly 606 MHz behavior seen with an unusable 5/5/5 W CPU configuration.
The Gaming profile demonstrated single-core boost around 5.03 GHz.

## Secondary issues found

- `power-profiles-daemon` could overwrite governor/EPP policy and make tests
  appear profile-dependent. It was not the fundamental GPU ceiling, but it
  must not race the single-owner daemon.
- ASUS fan curves ramp progressively. Short Balanced-profile tests ended before
  cooling and Dynamic Boost stabilized.
- Suspend/resume could leave ASUS WMI and NVIDIA power policy out of sync when
  multiple tools reapplied settings. The daemon now applies Safe before sleep,
  waits for three successful GPU readiness checks after wake and requires a new
  cooling confirmation.
- Closing the lid or removing AC while a high-power policy is active now causes
  an immediate Safe fallback.
- An old FurMark GUI process from manual testing was found and terminated. The
  benchmark helper uses a dedicated process group and always restores Safe.

## Why this is not generic kernel behavior

The Linux kernel exposes ASUS WMI primitives but cannot safely infer a laptop's
OEM power-sharing and thermal policy. `nvidia-powerd` consumes an NVIDIA/firmware
NVPCF contract in proprietary userspace. A generic kernel or driver change
cannot replace incorrect model-specific firmware values without a verified DMI
quirk and per-model validation.

The appropriate upstream form is a narrowly scoped ASUS/NVIDIA quirk. Until
that exists, this project keeps the workaround reversible and exact-model-only.
Relevant upstream documentation and discussion:

- [NVIDIA Dynamic Boost on Linux](https://download.nvidia.com/XFree86/Linux-x86_64/575.64.03/README/dynamicboost.html)
- [NVIDIA open GPU kernel modules](https://github.com/NVIDIA/open-gpu-kernel-modules)
- [NVIDIA Dynamic Boost issue 435](https://github.com/NVIDIA/open-gpu-kernel-modules/issues/435)
- [asusctl](https://github.com/OpenGamingCollective/asusctl)
- [ArchWiki CPU frequency scaling](https://wiki.archlinux.org/title/CPU_frequency_scaling)

## Implemented layers

1. GPL DKMS module with exact DMI checks before module initialization and before
   every parameter callback write.
2. Root daemon as the only WMI/CPU-policy owner, with validation, rollback,
   VBIOS capability detection and suspend/resume guards.
3. MIT `LD_PRELOAD` filter scoped to one NVIDIA ioctl/command/protocol and a
   root-owned non-writable configuration file.
4. Restricted D-Bus policy and systemd sandbox.
5. ROG Control Center provider UI that renders advertised limits rather than
   hard-coding a 120 W machine.
6. DKMS auto-install, diagnostic tooling and reversible install/uninstall.

No NVIDIA firmware flash, NVIDIA binary patch or forced NVML power-limit write
is part of this project.
