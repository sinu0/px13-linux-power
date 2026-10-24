# Changelog

## 0.2.0 - unreleased

- Limits follow the platform profile, like G-Helper's modes: Fn+F5, ROG
  Control Center, `asusctl` and asusd's switching on AC and battery select
  them. Each mode lists the profiles it may use; the first whose requirements
  hold is applied, otherwise Safe. The daemon no longer sets the platform
  profile itself, and `px13ctl` drops the profile commands.
- Safe on battery keeps the platform profile and lowers only the EPP to
  `power`; switching to Quiet on battery is now asusd's job (Throttle policy
  on battery), which selects the quiet mode.
- Move model data out of the code: `daemon/models/hn7306wv.toml` holds the
  DMI match, the firmware ranges, the modes and the stock profiles (Safe,
  Quiet Gaming, Gaming), validated at startup and by `px13_powerd.py --check`.
  GPU clock ranges and power limits come from the NVIDIA driver.
- Personal policy is opt-in: `/etc/px13-powerd/profiles.d/*.toml` may change
  modes, profiles and the conditions for manual tuning. The 120 W profile and
  the barrel-adapter, open-lid and cooling-confirmation conditions moved to
  `examples/profiles.d/120w.toml`; there, confirming the cooling upgrades the
  performance mode from Gaming to 120 W. Stock Gaming and manual tuning
  require external power only.
- Requests return at once. Quick writes happen immediately; waiting for asusd
  and restarting `nvidia-powerd` run on a worker thread where newer work
  supersedes older work, so several slider moves cause one restart. State
  reads never wait for NVML or slow steps. `StateChanged` and an `applying`
  flag report progress.
- Use NVML (`python-nvidia-ml-py`) instead of `nvidia-smi` and
  `nvidia-settings`: clock offsets no longer need an Xwayland session, and
  telemetry no longer spawns a process every second.
- Manual GPU power adjusts the active profile instead of switching to a copy
  of the 120 W profile, which also forced CPU PPT to 5/25/35 W. Manual CPU and
  GPU changes are kept per profile; `px13ctl reset` drops them.
- The Quiet Gaming battery balance starts at the profile's own CPU limit
  instead of a fixed 70 % level (about 17 W instead of 15 W).
- Stopping the service applies Safe.
- Battery watch in every profile: on external power the daemon measures the
  battery from its stored energy and reports when the charger does not cover
  the load. A critical battery that keeps draining for a minute hibernates
  the system, or suspends it when hibernation is not set up, because the
  desktop's own low-battery action never triggers while the firmware reports
  "Charging".
- An empty gauge counts as critical even when `capacity_level` still reads
  "Low": the PX13 reports 0 Wh at that level, so the watch saw no drain and
  the rescue never armed.
- On USB-C the drain warning asks for a re-plug: a USB-C reset the embedded
  controller does not see as an unplug leaves the input off while the
  firmware reports "Charging".
- New `px13-notify` user service shows controller warnings, such as a
  battery draining on external power, as desktop notifications.
- Quiet Gaming lowers the EPP to `power` on battery: asusd often selects the
  quiet mode on battery, where gaming tuning only drained it faster.
- The battery balance no longer goes blind on an empty battery: the gauge
  then reads 0 Wh, so the controller follows the trend of the battery voltage
  and drops to its lowest level only while the voltage falls. When the battery
  drains even at the lowest level, the status says that the charger does not
  cover the load. The firmware keeps reporting external power and "Charging"
  in that case, so the desktop's low-battery action never triggers.
- D-Bus: rename the service to `org.asuslinux.TuningProvider` and implement
  the tuning provider API of the asusctl fork. Errors now carry a proper
  D-Bus error name instead of the name embedded in the message.
- Detect power supplies, battery and lid by type instead of fixed `AC0`,
  `BAT0` and `LID` paths, ignoring peripheral batteries such as a stylus;
  read the CPU temperature from `coretemp` and `zenpower` too.
- Add timeouts to every helper process; `asusctl` and `nvidia-smi` calls
  could previously block the daemon indefinitely.
- Split the daemon into configuration, hardware, NVIDIA, battery balance and
  policy modules, with unit tests that run the policy on a simulated sysfs
  tree (`make check`).
- Replace the RCC patch with the `tuning-provider` branch of the asusctl fork.
- Install `/etc/modprobe.d/px13-nvidia-rtd3.conf` forcing
  `NVreg_DynamicPowerManagement=0x02` (fine-grained RTD3). NVIDIA 615.78.08
  reports "Runtime D3 status: Disabled by default" on this unit: SMBIOS
  chassis type 31 "Convertible" is not in the driver's notebook list, and 615
  changed `rm_is_system_notebook()` from a live chassis/ACPI-battery check to
  a value cached at RM init, after which this unit is no longer detected as a
  notebook. The dGPU stayed in D0 at ~6.5 W on battery. 0x02 is the same mode
  the driver's default picks on a detected Ampere+ notebook, so the override
  stays correct after an upstream fix. Doctor checks the Runtime D3 status and
  points out when the override is no longer needed on newer drivers.
- Add the Quiet Gaming profile (`px13ctl quiet-gaming`): ASUS Quiet, Safe GPU
  WMI envelope, CPU PPT starting at 15/20/25 W, EPP `balance_performance`;
  allowed on battery, no cooling confirmation needed, and restored after
  suspend/resume and daemon restarts.
- Daemon: add a battery balance controller for Quiet Gaming, active only on
  external power, so gaming on a 100 W USB-C PD charger does not drain the
  battery. A PI controller runs every 5 s on the battery flow derived from
  the change of BAT0 `energy_now` over a sliding 30 s window (needs 15 s of
  history; held while the battery is `Full`/`Not charging`), targeting +4 W
  charging, moving a single 0..100 % power level (starting at 70 %) by at
  most 3 % per step; above 50 % it scales CPU SPL 12..25 W (SPPT = SPL+5,
  FPPT = SPL+10), below 50 % it lowers the NVIDIA GPU clock ceiling from
  unlimited down to 1200 MHz (GPU cut last, since it matters most for frame
  pacing). `px13ctl state` reports `battery_balance` and `power_source`
  (`battery`/`usb-c`/`adapter`); a manual CPU power change in Quiet Gaming
  becomes the controller's new SPL ceiling.
- Daemon: stop deriving the battery balance controller's flow from an
  averaged `power_now`/`status`. On this PX13 the firmware kept reporting
  status `Charging` with `power_now` around +30..39 W while `energy_now`
  was actually falling under a USB-C gaming load (a measured −35.3 W net
  flow, 23.30 → 22.79 Wh in 50 s); the old controller trusted the status
  and raised power to 100 %, the worst possible decision. The controller
  now reads the flow from the `energy_now` window described above instead.
- Daemon: detect USB-C PD power via online `ucsi-source-psy-*` power
  supplies (the ASUS barrel adapter shows only `AC0`); refuse Gaming and
  120 W on USB-C power, and fall back an active high-power profile to Safe
  when the source changes to USB-C.
- Daemon: switch `amd_pstate` to `active` at runtime when possible; in
  `active` mode use `powersave` + EPP (Safe on battery: EPP `power` + ASUS
  profile Quiet; Safe on AC: EPP `balance_power` + ASUS profile Balanced;
  Gaming/120 W: EPP `performance`); fall back to `schedutil` (never
  `powersave`) in `guided`/`passive` when `active` mode is unavailable; Safe
  no longer applies a hard 2 GHz frequency cap.
- Daemon: re-apply the correct Safe variant automatically on AC
  plug/unplug.
- Daemon: on battery, no longer restart `nvidia-powerd` or verify the GPU
  power ceiling, fixing a false `safe fallback failed: nvidia-powerd
  synchronization failed: ceiling 45 W, expected 80 W` error caused by the
  firmware's own ~45 W DC power limit and Dynamic Boost disablement.
- Daemon: avoid `nvidia-smi`/`nvidia-settings` calls while the dGPU is
  runtime-suspended; cache VBIOS power limits in the state file instead of
  querying them live.
- Installer: add opt-in `--enable-s0ix`, installing
  `nvidia/px13-nvidia-s0ix.conf` (`NVreg_EnableS0ixPowerManagement=1`) to
  `/etc/modprobe.d/`; rewrite argument parsing as a loop so
  `--install-deps` and `--enable-s0ix` combine in any order; warn (without
  failing) when `amd_pstate` is not `active`, recommending
  `amd_pstate=active` on the kernel command line.
- Uninstaller: remove the S0ix modprobe drop-in if present, with the same
  initramfs-regeneration note.
- Bump `PROJECT_VERSION` to 0.2.0 (`MODULE_VERSION` stays 5.0).
- Doctor: check `amd_pstate` status is `active`, policy0 EPP availability,
  and that the CPU is not pinned to its minimum frequency
  (`scaling_governor=powersave` while `amd_pstate` is not `active`); print
  informational EPP, governor, `scaling_max_freq`, platform profile, AC
  state, dGPU `runtime_status` and NVIDIA S0ix status; only query
  `nvidia-smi` when AC is online, since it wakes the dGPU otherwise.
- Docs: correct the FINDINGS.md attribution of the ~606 MHz CPU floor to
  `amd_pstate=guided` + `powersave` (not confirmed to be the 5/5/5 W CPU PPT
  test specifically); document the recommended `amd_pstate=active` kernel
  command line, battery behavior, `--enable-s0ix`, and an "after system
  updates" checklist in README.md/README.pl.md.
- Docs: document the Quiet Gaming battery balance controller and USB-C PD
  detection in README.md/README.pl.md; add a note that `asusd`'s
  `platform_profile_linked_epp` and `change_platform_profile_on_ac`/
  `change_platform_profile_on_battery` must be `false` in
  `/etc/asusd/asusd.ron`, or `asusd` overrides this daemon's EPP/platform
  profile; record the observed ~140 W USB-C draw and the NVIDIA `610.57.04`
  negative NVPCF cTGP offset behavior in FINDINGS.md; note in
  `rcc/README.md` that the distro `rog-control-center` 6.4.0 update dropped
  the patched GUI and that the patch now targets `asusctl` 6.4.0.

## 0.1.0 - 2026-07-16

- Add exact-DMI ASUS WMI DKMS module and single-owner power daemon.
- Add VBIOS-aware Safe, Gaming and optional 120 W profiles.
- Correct PX13 NVPCF cTGP/Dynamic Boost response for `nvidia-powerd`.
- Add CPU PPT, GPU power, temperature and clock controls.
- Add suspend/resume, AC, lid and cooling-confirmation guards.
- Add hardened systemd/D-Bus policy, diagnostics and reversible installer.
- Add optional ROG Control Center provider patch.
