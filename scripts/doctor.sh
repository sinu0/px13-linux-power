#!/bin/sh
# SPDX-License-Identifier: MIT
set -u

failed=0
check()
{
    description=$1
    shift
    if "$@" >/dev/null 2>&1; then
        printf '[OK]   %s\n' "$description"
    else
        printf '[FAIL] %s\n' "$description"
        failed=1
    fi
}

ac_online=unknown
for supply in /sys/class/power_supply/*; do
    [ -r "$supply/type" ] || continue
    [ "$(cat "$supply/type" 2>/dev/null)" = Mains ] || continue
    ac_online=$(cat "$supply/online" 2>/dev/null || true)
    break
done

vendor=$(cat /sys/class/dmi/id/sys_vendor 2>/dev/null || true)
board=$(cat /sys/class/dmi/id/board_name 2>/dev/null || true)
printf 'System: %s / %s\n' "$vendor" "$board"
check 'model file and personal profiles valid' \
    python3 /usr/local/lib/px13-powerd/px13_powerd.py --check
check 'px13_tgp_probe loaded' test -d /sys/module/px13_tgp_probe
if [ -r /sys/module/px13_tgp_probe/version ]; then
    printf 'Module: %s\n' "$(cat /sys/module/px13_tgp_probe/version)"
fi
check 'DKMS installed for running kernel' sh -c \
    "dkms status -m px13-tgp -v 5.0 -k '$(uname -r)' 2>/dev/null | grep -q installed"
check 'asusd active' systemctl is-active --quiet asusd.service
check 'px13-powerd active' systemctl is-active --quiet px13-powerd.service
check 'nvidia-powerd active' systemctl is-active --quiet nvidia-powerd.service
check 'power-profiles-daemon inactive' sh -c \
    '! systemctl is-active --quiet power-profiles-daemon.service'
check 'NVPCF preload installed' test -r \
    /usr/local/lib/px13-powerd/libnvpcf_override.so
check 'NVPCF config is root-owned' sh -c \
    "test \"$(stat -c '%U:%a' /run/px13-powerd/nvpcf-override.conf 2>/dev/null)\" = root:600"
check 'amd_pstate active' sh -c \
    'test "$(cat /sys/devices/system/cpu/amd_pstate/status 2>/dev/null)" = active'
check 'policy0 EPP available' test -r \
    /sys/devices/system/cpu/cpufreq/policy0/energy_performance_preference
check 'NVIDIA runtime D3 enabled' sh -c \
    'grep -h "Runtime D3 status:" /proc/driver/nvidia/gpus/*/power 2>/dev/null | grep -q Enabled'
check 'CPU not pinned to minimum' sh -c '
    pstate=$(cat /sys/devices/system/cpu/amd_pstate/status 2>/dev/null || true)
    gov=$(cat /sys/devices/system/cpu/cpufreq/policy0/scaling_governor 2>/dev/null || true)
    if [ "$gov" = powersave ] && [ "$pstate" != active ]; then
        exit 1
    fi
    exit 0
'

printf '\nPower state (informational):\n'
printf '  EPP: %s\n' \
    "$(cat /sys/devices/system/cpu/cpufreq/policy0/energy_performance_preference 2>/dev/null || echo n/a)"
printf '  governor: %s\n' \
    "$(cat /sys/devices/system/cpu/cpufreq/policy0/scaling_governor 2>/dev/null || echo n/a)"
printf '  scaling_max_freq: %s\n' \
    "$(cat /sys/devices/system/cpu/cpufreq/policy0/scaling_max_freq 2>/dev/null || echo n/a)"
printf '  platform profile: %s\n' \
    "$(cat /sys/firmware/acpi/platform_profile 2>/dev/null || echo n/a)"
printf '  AC online: %s\n' "$ac_online"

gpu_dev=
for d in /sys/bus/pci/devices/*/; do
    [ -r "${d}vendor" ] && [ -r "${d}class" ] || continue
    v=$(cat "${d}vendor" 2>/dev/null || true)
    [ "$v" = "0x10de" ] || continue
    c=$(cat "${d}class" 2>/dev/null || true)
    case "$c" in
        0x03*) gpu_dev=$d ;;
    esac
    [ -n "$gpu_dev" ] && break
done
if [ -n "$gpu_dev" ] && [ -r "${gpu_dev}power/runtime_status" ]; then
    printf '  dGPU (%s) runtime_status: %s\n' "$gpu_dev" "$(cat "${gpu_dev}power/runtime_status")"
else
    printf '  dGPU runtime_status: n/a\n'
fi

rtd3_status=
s0ix_status=
found_gpu_power=0
for gpu_power in /proc/driver/nvidia/gpus/*/power; do
    [ -r "$gpu_power" ] || continue
    found_gpu_power=1
    rtd3_status=$(sed -n 's/^Runtime D3 status:[[:space:]]*//p' "$gpu_power")
    s0ix_status=$(awk '/S0ix Power Management:/{f=1; next} f && /Status:/{print $2; exit}' "$gpu_power")
    break
done
if [ "$found_gpu_power" -eq 1 ]; then
    printf '  Runtime D3 status: %s\n' "${rtd3_status:-unknown}"
    # The RTD3 override works around NVIDIA 615.78.08 not detecting this unit
    # as a notebook. Forcing 0x02 matches what a fixed driver picks by default,
    # so it is harmless later; flag newer drivers so it can be re-evaluated.
    nv_version=$(sed -n 's/.*Kernel Module for [^ ]*[[:space:]]*\([0-9.]*\).*/\1/p' \
        /proc/driver/nvidia/version 2>/dev/null)
    if [ -f /etc/modprobe.d/px13-nvidia-rtd3.conf ] &&
        [ -n "$nv_version" ] && [ "$nv_version" != 615.78.08 ]; then
        printf '  RTD3 override: driver %s is newer than 615.78.08; to test whether\n' "$nv_version"
        printf '    the override is still needed, move /etc/modprobe.d/px13-nvidia-rtd3.conf\n'
        printf '    away, run mkinitcpio -P, reboot and check for "Enabled (fine-grained)".\n'
    fi
    printf '  S0ix status: %s\n' "${s0ix_status:-unknown}"
else
    printf '  S0ix status: n/a\n'
fi

printf '\nNVIDIA limits:\n'
if [ "$ac_online" = 1 ]; then
    nvidia-smi --query-gpu=name,driver_version,power.default_limit,power.max_limit \
        --format=csv,noheader 2>/dev/null || failed=1
else
    printf 'skipped on battery (would wake the dGPU)\n'
fi
printf '\nController state:\n'
/usr/local/bin/px13ctl state 2>/dev/null || failed=1
printf '\nDKMS:\n'
dkms status 2>/dev/null | grep -E '(^|/)px13-tgp|^$' || true

exit "$failed"
