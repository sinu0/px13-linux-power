#!/bin/sh
# SPDX-License-Identifier: MIT
set -eu

MODULE_VERSION="5.0"
STATE_DIR=/var/lib/px13-powerd
INSTALL_STATE="$STATE_DIR/install-state"

[ "$(id -u)" -eq 0 ] || {
    printf 'px13-linux-power: run this uninstaller with sudo\n' >&2
    exit 1
}

if [ -x /usr/local/bin/px13ctl ]; then
    /usr/local/bin/px13ctl safe >/dev/null 2>&1 || true
fi
systemctl disable --now px13-powerd.service >/dev/null 2>&1 || true
systemctl stop nvidia-powerd.service >/dev/null 2>&1 || true
modprobe -r px13_tgp_probe >/dev/null 2>&1 || true

rm -f /etc/systemd/system/px13-powerd.service \
    /etc/systemd/system/nvidia-powerd.service.d/px13-nvpcf.conf \
    /etc/dbus-1/system.d/org.asuslinux.Px13Power.conf \
    /etc/modules-load.d/px13-tgp.conf \
    /usr/lib/systemd/system-sleep/px13-powerd \
    /usr/local/bin/px13ctl
rm -rf /usr/local/lib/px13-powerd

if dkms status -m px13-tgp -v "$MODULE_VERSION" 2>/dev/null | grep -q .; then
    dkms remove -m px13-tgp -v "$MODULE_VERSION" --all
fi
rm -rf "/usr/src/px13-tgp-$MODULE_VERSION"

systemctl daemon-reload
systemctl reload dbus.service 2>/dev/null || true
systemctl start nvidia-powerd.service 2>/dev/null || true

if [ -f "$INSTALL_STATE" ]; then
    # shellcheck disable=SC1090
    . "$INSTALL_STATE"
    if systemctl list-unit-files power-profiles-daemon.service --no-legend 2>/dev/null |
        grep -q '^power-profiles-daemon.service'; then
        systemctl unmask power-profiles-daemon.service >/dev/null 2>&1 || true
        case "${PPD_ENABLED:-unknown}" in
            enabled) systemctl enable power-profiles-daemon.service >/dev/null 2>&1 || true ;;
            disabled) systemctl disable power-profiles-daemon.service >/dev/null 2>&1 || true ;;
            masked) systemctl mask power-profiles-daemon.service >/dev/null 2>&1 || true ;;
        esac
        if [ "${PPD_ACTIVE:-inactive}" = active ]; then
            systemctl start power-profiles-daemon.service >/dev/null 2>&1 || true
        fi
    fi
fi
rm -rf "$STATE_DIR"
printf 'PX13 Linux Power removed. nvidia-powerd was restored without the override.\n'
