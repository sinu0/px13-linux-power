# PX13 Linux Power

Experimental, model-scoped CPU/GPU power control for the ASUS ProArt PX13
`HN7306WV`. It exposes Safe, Gaming and hardware-dependent maximum profiles,
plus CPU PPT, NVIDIA power, temperature and clock controls.

This project exists because the tested PX13 firmware returns lower NVIDIA
NVPCF values to Linux than the values selected through ASUS WMI. It does not
flash a GPU VBIOS and does not patch NVIDIA driver binaries.

## Supported system

- ASUS board name: `HN7306WV`
- Proprietary NVIDIA Linux driver with `nvidia-powerd`
- `asusctl`/`asusd`
- A DKMS-capable kernel with matching headers
- systemd and system D-Bus

The installer, kernel module, daemon and NVIDIA filter all enforce the exact
DMI vendor/board pair. Other PX13 variants are intentionally rejected until
their WMI and NVPCF behavior is independently verified.

The daemon reads `power.max_limit` from the installed NVIDIA firmware. A 120 W
profile is advertised only when that firmware reports at least 120 W. This
repository does not include or install a custom VBIOS.

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
packages and run `sudo ./install.sh`.

The installer:

- builds and installs `px13-tgp/5.0` with DKMS for every installed kernel that
  has headers;
- installs a narrowly scoped NVIDIA NVPCF response filter;
- disables and masks `power-profiles-daemon` while this controller is active;
- refuses installation when TLP, auto-cpufreq or LACT is active;
- starts in `Safe` and leaves overclock offsets at zero.

## Use

```bash
px13ctl state
px13ctl gaming
px13ctl confirm-cooling
px13ctl 120w
px13ctl safe
sudo ./scripts/doctor.sh
```

Gaming requires AC power and an open lid. The 120 W profile and manual
CPU/GPU/clock tuning additionally require a fresh cooling confirmation.
Confirmation is cleared after suspend/resume, daemon restart, AC removal, lid
closure or a manual return to Safe. Keep the laptop open and ensure
unobstructed intake and exhaust airflow.

ROG Control Center integration is supplied as an MPL-2.0 patch in [`rcc/`](rcc/).
The CLI and D-Bus backend work without the GUI patch.

## Kernel updates

DKMS has `AUTOINSTALL=yes`. A new kernel receives the module automatically if
its matching headers are installed. Reboot after a kernel update, then run:

```bash
dkms status
sudo ./scripts/doctor.sh
```

The service checks that the module exists for the running kernel and is loaded.
If either check fails, it does not start and therefore cannot apply a profile.

## Remove

```bash
sudo ./uninstall.sh
```

The uninstaller applies Safe first, removes DKMS and the NVIDIA drop-in, and
restores the pre-install `power-profiles-daemon` enabled/active state.

## Design and limitations

See [`docs/FINDINGS.md`](docs/FINDINGS.md) for measurements and the root cause.
The NVPCF filter accepts only one exact NVIDIA RM ioctl and known protocol
versions. A future NVIDIA update can change that ABI; in that case the filter
fails closed and power falls back to the firmware-provided limit. Run the
doctor after NVIDIA driver updates.

This is an experimental power-control project. Firmware, VRM, battery,
connector and thermal limits still apply. No software profile guarantees a
particular sustained wattage or game performance.

## Licenses

- Kernel module: GPL-2.0-only
- Daemon, tools and NVPCF filter: MIT
- ROG Control Center patch: MPL-2.0

Full texts are in [`LICENSES/`](LICENSES/).
