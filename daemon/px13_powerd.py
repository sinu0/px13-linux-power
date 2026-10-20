#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""px13-powerd: D-Bus front end of the power controller.

It implements the tuning provider API that ROG Control Center reads
(org.asuslinux.TuningProvider, see rcc/README.md) on top of
px13power.controller, which owns all policy.
"""

import json
import logging
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib, GLibUnix

from px13power import config, hardware
from px13power.controller import Controller, RequestError
from px13power.nvidia import Nvidia

BUS_NAME = "org.asuslinux.TuningProvider"
OBJECT_PATH = "/org/asuslinux/TuningProvider"
INTERFACE = "org.asuslinux.TuningProvider"
ERROR = f"{INTERFACE}.Error"
MODEL_DIR = Path(__file__).resolve().parent / "models"
OVERLAY_DIR = Path("/etc/px13-powerd/profiles.d")


class Service(dbus.service.Object):
    def __init__(self, bus, controller):
        self.controller = controller
        self.bus_name = dbus.service.BusName(BUS_NAME, bus=bus)
        super().__init__(self.bus_name, OBJECT_PATH)
        controller.on_change = lambda: GLib.idle_add(self._emit_state)

    def _emit_state(self):
        self.StateChanged(json.dumps(self.controller.state(), sort_keys=True))
        return False

    @staticmethod
    def _reply(request, *args):
        try:
            return json.dumps(request(*args), sort_keys=True)
        except (RequestError, hardware.HardwareError) as error:
            raise dbus.exceptions.DBusException(str(error), name=ERROR) from error

    @dbus.service.method(INTERFACE, in_signature="", out_signature="s")
    def GetCapabilities(self):
        return self._reply(self.controller.capabilities)

    @dbus.service.method(INTERFACE, in_signature="", out_signature="s")
    def GetState(self):
        return self._reply(self.controller.state)

    @dbus.service.method(INTERFACE, in_signature="", out_signature="s")
    def Reset(self):
        return self._reply(self.controller.reset)

    @dbus.service.method(INTERFACE, in_signature="", out_signature="s")
    def ConfirmCooling(self):
        return self._reply(self.controller.confirm_cooling)

    @dbus.service.method(INTERFACE, in_signature="iii", out_signature="s")
    def SetCpuPower(self, sustained, slow, fast):
        return self._reply(self.controller.set_cpu_power, sustained, slow, fast)

    @dbus.service.method(INTERFACE, in_signature="iii", out_signature="s")
    def SetGpuPower(self, base, boost, temperature):
        return self._reply(self.controller.set_gpu_power, base, boost, temperature)

    @dbus.service.method(INTERFACE, in_signature="ii", out_signature="s")
    def SetGpuOffsets(self, core, memory):
        return self._reply(self.controller.set_gpu_offsets, core, memory)

    @dbus.service.method(INTERFACE, in_signature="i", out_signature="s")
    def SetGpuClockLimit(self, limit):
        return self._reply(self.controller.set_gpu_clock_limit, limit)

    # Called by the system-sleep hook; logind's PrepareForSleep covers the
    # same events, and whichever arrives first wins.
    @dbus.service.method(INTERFACE, in_signature="", out_signature="s")
    def PrepareSuspend(self):
        return self._reply(self.controller.prepare_suspend, "system-sleep hook")

    @dbus.service.method(INTERFACE, in_signature="", out_signature="s")
    def ResumeAfterSleep(self):
        return self._reply(self.controller.resume, "system-sleep hook")

    @dbus.service.signal(INTERFACE, signature="s")
    def StateChanged(self, state):
        pass


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    log = logging.getLogger("px13-powerd")
    try:
        model = config.load(MODEL_DIR, OVERLAY_DIR, *hardware.dmi_identity())
    except config.ConfigError as error:
        log.error("%s", error)
        sys.exit(1)
    log.info("model %s/%s, profiles: %s", model.sys_vendor, model.board_name, ", ".join(model.profiles))
    if sys.argv[1:] == ["--check"]:
        return

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    hw = SimpleNamespace(
        supplies=hardware.PowerSupplies(),
        cpu=hardware.Cpu(),
        wmi=hardware.Wmi(),
        nvidia=Nvidia(),
        lid_open=hardware.lid_open,
        platform_profile=hardware.platform_profile,
        sleep_system=hardware.sleep_system,
    )
    controller = Controller(model, hw, log)
    Service(bus, controller)
    bus.add_signal_receiver(
        lambda sleeping: controller.prepare_suspend("logind") if sleeping else controller.resume("logind"),
        signal_name="PrepareForSleep",
        dbus_interface="org.freedesktop.login1.Manager",
    )
    # asusd announces platform profile changes (Fn+F5, ROG Control Center,
    # asusctl, its own AC/battery switching); the guard would notice within a
    # second, this makes the switch immediate.
    bus.add_signal_receiver(
        lambda *_: controller.evaluate(),
        signal_name="PropertiesChanged",
        dbus_interface="org.freedesktop.DBus.Properties",
        bus_name="xyz.ljones.Asusd",
        path="/xyz/ljones",
    )
    loop = GLib.MainLoop()

    def stop():
        controller.shutdown()
        loop.quit()
        return GLib.SOURCE_REMOVE

    GLibUnix.signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, stop)
    controller.start()
    loop.run()


if __name__ == "__main__":
    main()
