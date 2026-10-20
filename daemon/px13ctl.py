#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Command-line client for px13-powerd."""

import argparse
import json
import sys

import dbus

BUS_NAME = "org.asuslinux.TuningProvider"
OBJECT_PATH = "/org/asuslinux/TuningProvider"
INTERFACE = "org.asuslinux.TuningProvider"

# command -> (D-Bus method, help)
COMMANDS = {
    "state": ("GetState", "show the controller state"),
    "capabilities": ("GetCapabilities", "show the profiles and limits"),
    "reset": ("Reset", "drop manual changes of the active profile"),
    "confirm-cooling": ("ConfirmCooling", "confirm that the cooling is unobstructed"),
    "prepare-suspend": ("PrepareSuspend", "used by the system-sleep hook"),
    "resume-after-sleep": ("ResumeAfterSleep", "used by the system-sleep hook"),
}


def main():
    parser = argparse.ArgumentParser(prog="px13ctl")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, (_, help_text) in COMMANDS.items():
        commands.add_parser(name, help=help_text)
    commands.add_parser("activate", help="switch to a profile").add_argument("profile")
    commands.add_parser("default", help="switch to the default (safe) profile")
    args = parser.parse_args()

    controller = dbus.Interface(dbus.SystemBus().get_object(BUS_NAME, OBJECT_PATH), INTERFACE)
    try:
        if args.command == "activate":
            result = controller.Activate(args.profile)
        elif args.command == "default":
            capabilities = json.loads(str(controller.GetCapabilities()))
            result = controller.Activate(capabilities["default_profile"])
        else:
            result = getattr(controller, COMMANDS[args.command][0])()
    except dbus.exceptions.DBusException as error:
        sys.exit(f"px13ctl: {error.get_dbus_message()}")
    print(json.dumps(json.loads(str(result)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
