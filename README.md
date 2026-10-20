# PX13 Linux Power

Experimental, model-scoped CPU/GPU power control for the ASUS ProArt PX13
`HN7306WV`. It gives the GPU the power budget the same firmware uses on
Windows, with Safe, Quiet Gaming and Gaming profiles plus CPU PPT, NVIDIA
power, temperature and clock controls. Personal policies, such as a 120 W
profile for a modified VBIOS, are opt-in configuration.

This project exists because the tested PX13 firmware returns lower NVIDIA
NVPCF values to Linux than the values selected through ASUS WMI. It does not
flash a GPU VBIOS and does not patch NVIDIA driver binaries.

## Supported system

- ASUS board name: `HN7306WV`
- Proprietary NVIDIA Linux driver with `nvidia-powerd`
- `asusctl`/`asusd`
- A DKMS-capable kernel with matching headers
- systemd and system D-Bus

The kernel module and NVIDIA filter enforce the exact DMI vendor/board pair,
and the installer and daemon require a matching model file. Other PX13
variants are intentionally rejected until their WMI and NVPCF behavior is
independently verified; see [Adding a model](#adding-a-model).

The daemon reads `power.max_limit` from the installed NVIDIA firmware and
offers only profiles that fit it. This repository does not include or install
a custom VBIOS.

## Install

On Arch Linux, install the prerequisites first:

```bash
sudo pacman -S --needed base-devel dkms python-dbus python-gobject nvidia-utils asusctl
sudo pacman -S --needed "$(cat /usr/lib/modules/$(uname -r)/pkgbase)-headers"
git clone https://github.com/sinu0/px13-linux-power.git
cd px13-linux-power
sudo ./install.sh
```

Alternatively, `sudo ./install.sh --install-deps` installs the Arch packages
and then performs installation. On other distributions, install equivalent
packages and run `sudo ./install.sh`. The two flags `--install-deps` and
`--enable-s0ix` (below) can be combined in any order.

The installer:

- builds and installs `px13-tgp/5.0` with DKMS for every installed kernel that
  has headers;
- installs a narrowly scoped NVIDIA NVPCF response filter;
- disables and masks `power-profiles-daemon` while this controller is active;
- refuses installation when TLP, auto-cpufreq or LACT is active;
- warns (without failing) if the kernel command line lacks `amd_pstate=active`;
- starts in `Safe` and leaves overclock offsets at zero.

### Recommended kernel command line

Add `amd_pstate=active` to the kernel command line (replacing `guided`, if
present). In `guided`/`passive` mode the EPP sysfs files do not exist and the
0.1.0 Safe profile's `powersave` governor pinned the CPU near its minimum
frequency (~605 MHz) under load, independent of the CPU PPT wattage. In
`active` mode the daemon uses `powersave` + EPP; if `active` mode is not
available it falls back to `schedutil`, never to a bare `powersave` governor,
in `guided`/`passive`. `sudo ./scripts/doctor.sh` reports the current
`amd_pstate` status and warns if the CPU is pinned to its minimum frequency.
The daemon also switches `amd_pstate` to `active` at startup, so the command
line only matters before the daemon starts and after it is uninstalled.

### Optional: NVIDIA S0ix power management

This board only supports s2idle suspend, not hardware S3. `sudo ./install.sh
--enable-s0ix` installs `/etc/modprobe.d/px13-nvidia-s0ix.conf`, setting
`NVreg_EnableS0ixPowerManagement=1` for the NVIDIA module. This is opt-in
because it changes dGPU suspend behavior; after installing it you must
regenerate the initramfs (e.g. `sudo mkinitcpio -P`) and reboot for it to take
effect. `sudo ./uninstall.sh` removes the file if present and prints the same
initramfs/reboot note.

## Use

```bash
px13ctl state
px13ctl capabilities
px13ctl activate "Quiet Gaming"
px13ctl activate Gaming
px13ctl reset            # drop manual changes of the active profile
px13ctl default          # back to Safe
sudo ./scripts/doctor.sh
```

### Profiles

The stock profiles live in the model file
[`daemon/models/hn7306wv.toml`](daemon/models/hn7306wv.toml):

- **Safe** is the default. It is applied at startup, before suspend and
  whenever the conditions of another profile stop holding.
- **Quiet Gaming** keeps the ASUS Quiet fan profile and the Safe GPU WMI
  envelope, and starts the CPU at PPT 15/20/25 W with the
  `balance_performance` EPP. It works on battery and is restored after
  suspend/resume and daemon restarts.
- **Gaming** requires external power.

Manual CPU/GPU/clock tuning (ROG Control Center sliders) is possible in any
profile except Safe, on external power. Changes are kept per profile;
`px13ctl reset` drops them. Profiles never overclock on their own.

### Personal profiles

Conditions such as "only on the barrel adapter" or "only with the lid open",
and profiles for a modified VBIOS, are personal policy, so they are not
defaults. Files in `/etc/px13-powerd/profiles.d/*.toml` may add profiles,
change any key of an existing one and set the conditions for manual tuning.
They are validated against the model's firmware ranges at startup and by
`sudo /usr/local/lib/px13-powerd/px13_powerd.py --check`.

A profile or `[tuning]` can require:

| Requirement            | Meaning                                         |
| ---------------------- | ----------------------------------------------- |
| `ac`                   | any external power, including USB-C             |
| `adapter`              | external power that is not USB-C PD             |
| `lid_open`             | the lid is open                                 |
| `cooling_confirmation` | confirmed in ROG Control Center or with `px13ctl confirm-cooling` |

When a requirement of the active profile stops holding, the daemon falls back
to Safe. Cooling confirmation is checked when a profile is chosen and cleared
by suspend/resume, a daemon restart, any fallback and a manual return to
Safe; confirming needs external power and an open lid.

[`examples/profiles.d/120w.toml`](examples/profiles.d/120w.toml) is a
complete example: Gaming only on the barrel adapter with the lid open, a 120 W
profile that also needs cooling confirmation, and the same conditions for
manual tuning. The 120 W profile is offered only when the installed VBIOS
reports at least 120 W.

```bash
sudo install -Dm644 examples/profiles.d/120w.toml /etc/px13-powerd/profiles.d/120w.toml
sudo systemctl restart px13-powerd
```

### ROG Control Center

The controls appear in ROG Control Center with a small asusctl fork that adds
tuning provider support; see [`rcc/`](rcc/). The CLI and D-Bus backend work
without it.

### Quiet Gaming battery balance

On external power, Quiet Gaming runs a PI controller that trades CPU PPT and
the NVIDIA GPU clock ceiling to keep the battery gently charging while
gaming — the target case is a 100 W USB-C PD charger, which cannot supply the
laptop's full Gaming/120 W draw at once, as opposed to the 200 W ASUS barrel
adapter. On battery power the controller pauses.

Every 5 seconds the controller derives the battery flow from the change of
BAT0 `energy_now` over a sliding 30-second window (it needs at least 15 s of
history before it produces a reading, and the reading is held while the
battery reports `Full`/`Not charging`), and compares it to a setpoint of +4 W
charging. It does not use `power_now`/`status`: on this hardware the firmware
has been observed reporting status `Charging` with `power_now` around
+30..39 W while `energy_now` was actually falling — a real flow of about
−35 W — under a USB-C gaming load; a controller that trusted status/power_now
would raise power to 100 % in exactly that situation, the worst possible
response (see [`docs/FINDINGS.md`](docs/FINDINGS.md)). Because `energy_now`
only has whole-µWh granularity, a 30 s window reading changes in steps of
roughly 1.9 W rather than smoothly. It moves a single power level from 0 to
100 % (starting at 70 %) by at most 3 % per 5-second step, so a brief 0 W or
slightly negative (discharge) reading from a game's power spike is absorbed
by the battery instead of instantly cutting clocks — the controller is tuned
for smoothness, holding the *average* rather than reacting to every sample.
Above 50 % the level
scales the CPU sustained power (SPL) from 12 to 25 W (SPPT = SPL + 5 W,
FPPT = SPL + 10 W); below 50 % it instead lowers the NVIDIA GPU clock ceiling
(`nvidia-smi --lock-gpu-clocks`) from unlimited down to 1200 MHz. The GPU
clock is cut last, after the CPU is already at its floor, because it matters
most for frame pacing. When the battery is full or not charging there is no
charge/discharge signal to react to, so the level is held at its last value.
A manual CPU power change (the ROG Control Center CPU sliders) while Quiet
Gaming is active becomes the controller's new SPL ceiling instead of being
overridden on the next step. The controller owns the GPU clock limit, so
manual clock controls are disabled in Quiet Gaming.

`px13ctl state` reports the controller under `battery_balance`
(`level_percent`, `cpu_spl_w`, `gpu_clock_limit_mhz`, `battery_net_w`) and reports the detected power source under `power_source`
(`battery`, `usb-c` or `adapter`).

### USB-C PD detection

The daemon tells a USB-C PD charger apart from the ASUS barrel adapter by
checking whether any `ucsi-source-psy-*` power supply is online; the barrel
adapter only ever shows the mains supply online. A profile that requires
`adapter` is refused on USB-C power, and the daemon falls back to Safe when
the source changes to USB-C while it is active. A 100 W USB-C PD charger
cannot supply Gaming: the battery drained by about 40 W in tests, so the
example personal profiles require the adapter; Quiet Gaming is the profile
for USB-C charging.

### asusd interference

If `asusd`'s own `platform_profile_linked_epp` or
`change_platform_profile_on_ac`/`change_platform_profile_on_battery` options
are enabled, `asusd` can overwrite the EPP or platform profile that this
daemon just set (for example forcing EPP back to `power` in Quiet Gaming, or
to Performance right after plugging in AC). Set all three options to `false`
in `/etc/asusd/asusd.ron`, stopping `asusd` first — it rewrites that file on
exit and would otherwise discard the edit.

## Battery behavior

Safe automatically switches variant when AC is plugged or unplugged:

- On battery: ASUS profile Quiet, EPP `power`. The daemon does not restart
  `nvidia-powerd` and does not verify the GPU power ceiling on battery,
  because the NVIDIA firmware itself limits the dGPU to about 45 W and
  disables Dynamic Boost on DC power; checking for the AC ceiling here
  previously produced a false `safe fallback failed: nvidia-powerd
  synchronization failed: ceiling 45 W, expected 80 W` error. The dGPU is
  intentionally left runtime-suspended: the daemon avoids `nvidia-smi` and
  `nvidia-settings` calls on battery, since either one wakes the dGPU from
  D3cold for over 20 seconds. VBIOS power limits are cached in the daemon's
  state file at install/first-AC-read time so they do not need a live query.
- On AC: ASUS profile Balanced, EPP `balance_power`.
- Gaming and the example 120 W profile: EPP `performance`.

## After system updates

Run this checklist after any of: a kernel update, an NVIDIA driver update, or
an `asusctl`/`asusd` update.

```bash
dkms status
sudo ./scripts/doctor.sh
```

- `dkms status` should show `px13-tgp/5.0` installed for the running kernel.
  DKMS has `AUTOINSTALL=yes`, so a new kernel receives the module
  automatically if its matching headers are installed; the service checks
  that the module exists for the running kernel and is loaded, and refuses to
  start (rather than apply a stale profile) if either check fails.
- The doctor reports `amd_pstate` status, EPP, governor, platform profile, AC
  state and dGPU runtime state; review any `[FAIL]` lines.
- After an NVIDIA driver update specifically, also check the `nvidia-powerd`
  journal for confirmation that the NVPCF filter is still active:

  ```bash
  journalctl -u nvidia-powerd.service | grep 'PX13 NVPCF override'
  ```

  If that line is missing after a driver update, the filter may have failed
  closed against a changed RM ioctl ABI; power then falls back to the
  firmware-provided (non-120 W) limit until the filter is revalidated.

## Remove

```bash
sudo ./uninstall.sh
```

The uninstaller applies Safe first, removes DKMS and the NVIDIA drop-in, and
restores the pre-install `power-profiles-daemon` enabled/active state.
Personal profiles in `/etc/px13-powerd` are kept.

## Design and limitations

See [`docs/FINDINGS.md`](docs/FINDINGS.md) for measurements and the root cause.
The NVPCF filter accepts only one exact NVIDIA RM ioctl and known protocol
versions. A future NVIDIA update can change that ABI; in that case the filter
fails closed and power falls back to the firmware-provided limit. Run the
doctor after NVIDIA driver updates.

This is an experimental power-control project. Firmware, VRM, battery,
connector and thermal limits still apply. No software profile guarantees a
particular sustained wattage or game performance.

## Adding a model

The daemon runs only on systems with a verified model file. Supporting another
board needs:

1. its DMI board name in the kernel module's `px13_dmi_table`, after checking
   that its firmware uses the same WMI device IDs;
2. a model file in `daemon/models/` with the ranges measured on that board
   and its stock profiles;
3. `make check`, then a measured run with `px13_benchmark.py`.

## Development

```bash
make check    # syntax, unit tests, NVPCF filter and kernel module builds
python3 -m unittest discover -s tests
```

The daemon is split into a configuration loader (`config.py`), thin hardware
interfaces (`hardware.py`, `nvidia.py`), the battery balance controller
(`balance.py`) and the policy (`controller.py`), which knows nothing about
D-Bus. The tests run the policy against a simulated sysfs tree.

## Licenses

- Kernel module: GPL-2.0-only
- Daemon, tools and NVPCF filter: MIT

Full texts are in [`LICENSES/`](LICENSES/).
