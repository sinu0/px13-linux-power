#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""px13-notify: desktop notifications for px13-powerd warnings.

px13-powerd runs as root on the system bus and cannot reach the desktop. This
user service follows its StateChanged signal and shows the controller's last
error, such as a battery draining on external power, as a notification.
"""

import json

BUS_NAME = "org.asuslinux.TuningProvider"
OBJECT_PATH = "/org/asuslinux/TuningProvider"
INTERFACE = "org.asuslinux.TuningProvider"
APP_NAME = "PX13 Power"


class Notifier:
    """Shows each new error once and withdraws it when it clears."""

    def __init__(self, show, close):
        self._show = show  # (message, id to replace or 0) -> notification id
        self._close = close  # notification id
        self._error = ""
        self._id = 0

    def update(self, state):
        error = state.get("last_error", "")
        if error == self._error:
            return
        self._error = error
        if error:
            self._id = self._show(error, self._id)
        elif self._id:
            self._close(self._id)
            self._id = 0


def main():
    import dbus
    import dbus.mainloop.glib
    from gi.repository import GLib

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    system, session = dbus.SystemBus(), dbus.SessionBus()
    notifications = dbus.Interface(
        session.get_object("org.freedesktop.Notifications", "/org/freedesktop/Notifications"),
        "org.freedesktop.Notifications",
    )
    notifier = Notifier(
        show=lambda message, replaces: int(notifications.Notify(
            APP_NAME, dbus.UInt32(replaces), "battery-caution", APP_NAME, message,
            dbus.Array([], signature="s"), {"urgency": dbus.Byte(2)}, -1,
        )),
        close=lambda notification: notifications.CloseNotification(dbus.UInt32(notification)),
    )
    system.add_signal_receiver(
        lambda state: notifier.update(json.loads(state)),
        signal_name="StateChanged",
        dbus_interface=INTERFACE,
        bus_name=BUS_NAME,
        path=OBJECT_PATH,
    )
    try:
        notifier.update(json.loads(system.get_object(BUS_NAME, OBJECT_PATH).GetState(dbus_interface=INTERFACE)))
    except dbus.exceptions.DBusException:
        pass  # px13-powerd is not running or refuses the call; its signals still arrive
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
