#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Command-line client for px13-powerd."""

import argparse
import json

import dbus


BUS_NAME = "org.asuslinux.Px13Power"
OBJECT_PATH = "/org/asuslinux/Px13Power"
INTERFACE = "org.asuslinux.Px13Power"


def main():
    parser = argparse.ArgumentParser(prog="px13ctl")
    parser.add_argument(
        "command",
        choices=(
            "state",
            "confirm-cooling",
            "prepare-suspend",
            "resume-after-sleep",
            "safe",
            "gaming",
            "120w",
        ),
    )
    args = parser.parse_args()

    bus = dbus.SystemBus()
    controller = dbus.Interface(bus.get_object(BUS_NAME, OBJECT_PATH), INTERFACE)
    if args.command == "state":
        result = controller.GetState()
    elif args.command == "confirm-cooling":
        result = controller.ConfirmCooling()
    elif args.command == "prepare-suspend":
        result = controller.PrepareSuspend()
    elif args.command == "resume-after-sleep":
        result = controller.ResumeAfterSleep()
    else:
        profile = {"safe": "Safe", "gaming": "Gaming", "120w": "120W"}[args.command]
        result = controller.Activate(profile)
    print(json.dumps(json.loads(str(result)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
