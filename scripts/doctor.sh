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

vendor=$(cat /sys/class/dmi/id/sys_vendor 2>/dev/null || true)
board=$(cat /sys/class/dmi/id/board_name 2>/dev/null || true)
printf 'System: %s / %s\n' "$vendor" "$board"
check 'supported DMI' test "$vendor/$board" = 'ASUSTeK COMPUTER INC./HN7306WV'
check 'px13_tgp_probe loaded' test -d /sys/module/px13_tgp_probe
if [ -r /sys/module/px13_tgp_probe/version ]; then
    printf 'Module: %s\n' "$(cat /sys/module/px13_tgp_probe/version)"
fi
check 'DKMS installed for running kernel' sh -c \
    "dkms status -m px13-tgp -v 5.0 -k '$(uname -r)' 2>/dev/null | grep -q installed"
check 'px13-powerd active' systemctl is-active --quiet px13-powerd.service
check 'nvidia-powerd active' systemctl is-active --quiet nvidia-powerd.service
check 'power-profiles-daemon inactive' sh -c \
    '! systemctl is-active --quiet power-profiles-daemon.service'
check 'NVPCF preload installed' test -r \
    /usr/local/lib/px13-powerd/libnvpcf_override.so
check 'NVPCF config is root-owned' sh -c \
    "test \"$(stat -c '%U:%a' /run/px13-powerd/nvpcf-override.conf 2>/dev/null)\" = root:600"

printf '\nNVIDIA limits:\n'
nvidia-smi --query-gpu=name,driver_version,power.default_limit,power.max_limit \
    --format=csv,noheader 2>/dev/null || failed=1
printf '\nController state:\n'
/usr/local/bin/px13ctl state 2>/dev/null || failed=1
printf '\nDKMS:\n'
dkms status 2>/dev/null | grep -E '(^|/)px13-tgp|^$' || true

exit "$failed"
