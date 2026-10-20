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

## 2026-09 re-evaluation

Re-tested on kernel `7.3.0-rc1` (CachyOS), NVIDIA `610.57.04`, `asusctl`
6.4.0, with the boot cmdline using `amd_pstate=guided`.

- Correction to the "Measurements" section above: the roughly 606 MHz /
  605 MHz CPU behavior is now confirmed, on this kernel/driver combination, to
  be caused by `amd_pstate=guided` combined with the Safe profile's
  `powersave` governor. In `guided` mode `powersave` pins the CPU at its
  minimum P-state (~605 MHz) under load, regardless of the configured CPU
  PPT. The original text attributed "roughly 606 MHz behavior" to the 5/5/5 W
  CPU PPT test specifically; that attribution is at least partly wrong. The
  5/5/5 W run was not re-measured under `amd_pstate=active`, so whether it
  would show the same floor-clock behavior independent of governor is
  unknown — only the guided+powersave mechanism has been confirmed as a
  cause of the ~600 MHz lock.
- In `guided` (and `passive`) mode, the per-policy
  `energy_performance_preference` sysfs file does not exist at all. Both the
  daemon's EPP settings and asusd's `platform_profile_linked_epp` therefore
  had no effect while `amd_pstate` was in `guided` mode — there was nothing
  to write to.
- `amd_pstate=active` restores the EPP sysfs files and lets `powersave` +
  EPP behave as intended (EPP controls the target within the driver's own
  P-state range, instead of the generic cpufreq governor forcing the floor).
  Daemon 0.2.0 switches `amd_pstate` to `active` at runtime when possible and
  recommends `amd_pstate=active` on the kernel command line; when `active` is
  not available it uses `schedutil` rather than `powersave` in
  `guided`/`passive`, and Safe no longer applies a hard 2 GHz cap on top.
- Kernel 7.3 ships `asus_armoury` firmware attributes (`nv_tgp`,
  `ppt_pl1_spl`, ...) but they return `ENODEV` on `HN7306WV`, because the
  in-kernel driver has no power-limit table entry for this board. The DKMS
  module in this repository remains required until an upstream
  `asus-armoury` `power_data` entry for `HN7306WV` exists.
- On battery, the NVIDIA firmware itself limits the dGPU to about 45 W and
  disables Dynamic Boost; the pre-2026-09 daemon's post-fallback ceiling
  check (which expected the AC-level 80 W baseline) produced a false `safe
  fallback failed: nvidia-powerd synchronization failed: ceiling 45 W,
  expected 80 W` error on battery. 0.2.0 no longer restarts `nvidia-powerd`
  or verifies the GPU ceiling when running on battery, and avoids
  `nvidia-smi`/`nvidia-settings` calls while the dGPU is runtime-suspended,
  since either call wakes it from D3cold for more than 20 seconds. VBIOS
  power limits are cached in the daemon state file instead of being queried
  live every time.
- The NVPCF filter was re-confirmed working against NVIDIA `610.57.04`.
- Before the battery balance controller existed, running Gaming/Quiet Gaming
  on a 100 W USB-C PD charger drew approximately 140 W at the wall while the
  battery lost about 40 W, because the daemon applied the same WMI/NVPCF
  power limits regardless of the detected power source, and the laptop's own
  draw exceeded what the 100 W charger could supply. On a later occasion the
  machine then would not charge, even sitting idle, until it was hibernated
  and resumed; the exact cause is not confirmed — it is plausibly the
  charger or the EC's own overcurrent/overpower protection latching, but
  this has not been verified against any specific mechanism. This is the
  motivation for the battery balance controller and for refusing Gaming/
  120 W on USB-C power (see README.md).
- The battery balance controller's original sign source, an EMA of
  `power_now`/`status`, was itself wrong. Verified on this PX13 under a
  USB-C gaming load: the firmware kept reporting `status` `Charging` with
  `power_now` around +30..39 W while `energy_now` was actually falling — a
  real flow of −35.3 W (`energy_now` 23.30 → 22.79 Wh in 50 s). `status`/
  `power_now` therefore cannot provide the sign of the battery flow on this
  hardware; a controller trusting them raised power to 100 %, the worst
  possible response to an actual discharge. The controller now derives the
  flow from the change of BAT0 `energy_now` over a sliding 30 s window
  (`BALANCE_WINDOW_S`), needing at least 15 s of history
  (`BALANCE_MIN_WINDOW_S`) before it produces a reading, and holding the
  last reading while `status` is `Full`/`Not charging`. Because `energy_now`
  only has whole-µWh granularity, a 30 s window reading steps in increments
  of roughly 1.9 W rather than changing smoothly.
  Measured live, in the same game session, after switching to the
  `energy_now`-window controller: the battery flow went from −34 W to about
  0/−2 W within roughly 3 minutes as the controller cut CPU SPL to 12 W and
  then the GPU clock ceiling to about 1950..2010 MHz (GPU power about
  53..55 W), and temperatures dropped from GPU 88 °C / CPU 94 °C to about
  76 °C / 77 °C.
- NVIDIA `610.57.04` ignores a negative NVPCF cTGP offset: with the NVPCF
  filter asked to write a `-30000` mW cTGP offset, the reported GPU power
  ceiling stayed at 80 W instead of dropping. Watt-based GPU power capping
  below the VBIOS default is therefore not achievable through this filter on
  this driver version; the battery balance controller instead lowers the
  NVIDIA GPU clock ceiling (`nvidia-smi --lock-gpu-clocks`) to reduce GPU
  power. The filter continues to reject negative offsets rather than send
  one the driver silently ignores.
- `asusd`'s own `platform_profile_linked_epp` and
  `change_platform_profile_on_ac`/`change_platform_profile_on_battery`
  options raced this daemon and overrode its choices — for example EPP was
  forced back to `power` while Quiet Gaming was active, and to Performance
  immediately after plugging in AC. All three options are set to `false` in
  `/etc/asusd/asusd.ron` on the test machine; see the README for the
  procedure (stop `asusd` before editing, since it rewrites the file on
  exit).
- The distro `rog-control-center` 6.4.0 package replaced the patched GUI
  binary, which is why the CPU wattage sliders disappeared: stock RCC 6.4.0
  reads CPU PPT through `asus-armoury` firmware attributes, which return
  `ENODEV` on `HN7306WV` (see above). The GPU tuning provider patch in
  `rcc/` was ported to `asusctl` 6.4.0; the only required change was that
  the `init_property!`/`setup_external!` macros in 6.4.0 no longer take a
  type argument.

## 2026-10 deep discharge on USB-C

- With the quiet mode selected on battery, the 0.2.0 development daemon ran
  Quiet Gaming (EPP `balance_performance`) on battery and drained it to
  0 %. The firmware reported `AC0` online and `Charging` with a positive
  `power_now` on the USB-C charger the whole time, so the desktop's
  low-battery action never triggered. At 0 % the gauge reads 0 Wh, which
  also blinded the battery balance controller.
- The embedded controller then latched two limits at once. Under load the
  CPU stayed at about 606 MHz with Gaming PPT (30/45/55 W), full CPPC limits
  and `amd_pstate` active, so the throttling came from the firmware. On the
  user's usual 150 W charger and cable the battery lost about 12 W (stored
  energy 15.09 → 14.68 Wh in 2 minutes) while the firmware reported +8..17 W.
- A normal reboot cleared the CPU throttling but not the input limit. An EC
  reset (power off, unplug, hold the power button for 30-40 s) cleared it:
  the same charger and cable then charged at +72 W measured from the stored
  energy, matching the firmware's +70 W.
- The kernel received no PD source capabilities from the UCSI firmware, and
  its VBUS voltage and current readings stay at 0, so the negotiated contract
  could not be read.
- The daemon now runs Quiet Gaming with EPP `power` on battery, follows the
  battery voltage trend while the gauge reads empty, and reports when the
  battery drains even at the lowest limits on external power.
