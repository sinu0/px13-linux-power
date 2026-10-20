# SPDX-License-Identifier: MIT
"""Model description, profiles and modes.

A model file describes one verified laptop: the firmware ranges the daemon may
write, its stock profiles and which profiles each platform profile (mode) uses.
Files in the profiles.d directory may change modes, add or change profiles and
set the conditions for manual tuning, so personal policies stay out of the
defaults.
"""

import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

# Conditions a profile or manual tuning can require, with their descriptions.
REQUIREMENTS = {
    "ac": "external power",
    "adapter": "the power adapter (not USB-C)",
    "lid_open": "an open lid",
    "cooling_confirmation": "cooling confirmation",
}

# Profile keys that may differ while running from the battery.
BATTERY_KEYS = {"cpu_epp"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Range:
    min: int
    max: int
    step: int = 1

    def __contains__(self, value):
        return self.min <= value <= self.max and (value - self.min) % self.step == 0

    def describe(self, unit):
        step = f" in {self.step} {unit} steps" if self.step > 1 else ""
        return f"{self.min}..{self.max} {unit}{step}"

    def to_json(self):
        return {"min": self.min, "max": self.max, "step": self.step}


@dataclass(frozen=True)
class CpuLimits:
    sustained: Range
    slow: Range
    fast: Range

    def accepts(self, values):
        sustained, slow, fast = values
        return (
            sustained in self.sustained
            and slow in self.slow
            and fast in self.fast
            and sustained <= slow <= fast
        )

    def describe(self):
        return (
            f"sustained {self.sustained.describe('W')}, slow {self.slow.describe('W')}, "
            f"fast {self.fast.describe('W')}, with sustained <= slow <= fast"
        )


@dataclass(frozen=True)
class GpuLimits:
    tgp_offset: Range  # W on top of the firmware base TGP
    dynamic_boost: Range
    temperature: Range


@dataclass(frozen=True)
class BalanceLimits:
    cpu_spl: Range
    gpu_clock: Range


@dataclass(frozen=True)
class Profile:
    name: str
    platform_mode: int  # firmware thermal policy written with the budget
    gpu_tgp_offset: int
    gpu_dynamic_boost: int
    gpu_thermal_target: int
    cpu: tuple
    cpu_epp: str
    requires: frozenset = frozenset()
    battery_balance: bool = False
    on_battery: dict = field(default_factory=dict)

    def for_source(self, source):
        return replace(self, **self.on_battery) if source == "battery" else self


@dataclass(frozen=True)
class Model:
    name: str
    sys_vendor: str
    board_name: str
    default_profile: str
    profiles: dict  # name -> Profile
    modes: dict  # platform profile -> profile names, first usable wins
    tuning_requires: frozenset
    cpu: CpuLimits
    gpu: GpuLimits
    balance: BalanceLimits

    @property
    def requires_cooling_confirmation(self):
        return "cooling_confirmation" in self.tuning_requires or any(
            "cooling_confirmation" in profile.requires for profile in self.profiles.values()
        )


def describe_missing(missing):
    """Turns requirement keys into 'A, B and C'."""
    names = [REQUIREMENTS[key] for key in missing]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def load(model_dir, overlay_dir, sys_vendor, board_name):
    """Returns the model matching the DMI identity, with all overlays applied."""
    for path in sorted(Path(model_dir).glob("*.toml")):
        data = _read(path)
        match = data.get("match", {})
        if match.get("sys_vendor") != sys_vendor or match.get("board_name") != board_name:
            continue
        for overlay in sorted(Path(overlay_dir).glob("*.toml")):
            _merge(data, _read(overlay), overlay)
        return _parse(data, path)
    raise ConfigError(f"unsupported system {sys_vendor!r}/{board_name!r}: no model file")


def _read(path):
    try:
        with open(path, "rb") as file:
            return tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"{path}: {error}") from error


def _merge(data, overlay, path):
    """Overlays may change modes and manual tuning, and add or change profiles key by key."""
    unknown = set(overlay) - {"modes", "tuning", "profiles"}
    if unknown:
        raise ConfigError(f"{path}: only [modes], [tuning] and [profiles.*] may be overridden, not {sorted(unknown)}")
    data.setdefault("modes", {}).update(overlay.get("modes", {}))
    data.setdefault("tuning", {}).update(overlay.get("tuning", {}))
    profiles = data.setdefault("profiles", {})
    for name, values in overlay.get("profiles", {}).items():
        profiles.setdefault(name, {}).update(values)


def _range(data, where):
    try:
        value = Range(**data)
    except TypeError as error:
        raise ConfigError(f"{where}: expected {{min, max[, step]}}, got {data!r}") from error
    if value.min > value.max or value.step < 1:
        raise ConfigError(f"{where}: invalid range {data!r}")
    return value


def _requirements(values, where):
    unknown = set(values) - set(REQUIREMENTS)
    if unknown:
        raise ConfigError(f"{where}: unknown requirements {sorted(unknown)}; known: {sorted(REQUIREMENTS)}")
    return frozenset(values)


def _profile(name, data, model_limits, path):
    where = f"{path}: profile {name!r}"
    data = dict(data)
    try:
        profile = Profile(
            name=name,
            platform_mode=data.pop("platform_mode"),
            gpu_tgp_offset=data.pop("gpu_tgp_offset"),
            gpu_dynamic_boost=data.pop("gpu_dynamic_boost"),
            gpu_thermal_target=data.pop("gpu_thermal_target"),
            cpu=tuple(data.pop("cpu")),
            cpu_epp=data.pop("cpu_epp"),
            requires=_requirements(data.pop("requires", []), where),
            battery_balance=data.pop("battery_balance", False),
            on_battery=data.pop("on_battery", {}),
        )
    except KeyError as error:
        raise ConfigError(f"{where}: missing {error.args[0]!r}") from error
    if data:
        raise ConfigError(f"{where}: unknown keys {sorted(data)}")
    if set(profile.on_battery) - BATTERY_KEYS:
        raise ConfigError(f"{where}: on_battery may only set {sorted(BATTERY_KEYS)}")
    cpu, gpu = model_limits
    if len(profile.cpu) != 3 or not cpu.accepts(profile.cpu):
        raise ConfigError(f"{where}: cpu must fit {cpu.describe()}")
    if (
        profile.gpu_tgp_offset not in gpu.tgp_offset
        or profile.gpu_dynamic_boost not in gpu.dynamic_boost
        or profile.gpu_thermal_target not in gpu.temperature
    ):
        raise ConfigError(f"{where}: GPU values are outside the model's [gpu] ranges")
    return profile


def _parse(data, path):
    try:
        gpu = data["gpu"]
        cpu = CpuLimits(*(_range(data["cpu"][key], f"{path}: cpu.{key}") for key in ("sustained", "slow", "fast")))
        gpu_limits = GpuLimits(
            *(_range(gpu[key], f"{path}: gpu.{key}") for key in ("tgp_offset", "dynamic_boost", "temperature"))
        )
        balance = BalanceLimits(
            cpu_spl=_range(data["battery_balance"]["cpu_spl"], f"{path}: battery_balance.cpu_spl"),
            gpu_clock=_range(data["battery_balance"]["gpu_clock"], f"{path}: battery_balance.gpu_clock"),
        )
        profiles = {
            name: _profile(name, values, (cpu, gpu_limits), path)
            for name, values in data["profiles"].items()
        }
        model = Model(
            name=data["name"],
            sys_vendor=data["match"]["sys_vendor"],
            board_name=data["match"]["board_name"],
            default_profile=data["default_profile"],
            profiles=profiles,
            modes={mode: tuple(names) for mode, names in data["modes"].items()},
            tuning_requires=_requirements(data.get("tuning", {}).get("requires", []), f"{path}: tuning"),
            cpu=cpu,
            gpu=gpu_limits,
            balance=balance,
        )
    except KeyError as error:
        raise ConfigError(f"{path}: missing {error.args[0]!r}") from error
    default = model.profiles.get(model.default_profile)
    if default is None or default.requires:
        raise ConfigError(f"{path}: default profile {model.default_profile!r} must exist and have no requirements")
    for mode, names in model.modes.items():
        unknown = [name for name in names if name not in model.profiles]
        if unknown:
            raise ConfigError(f"{path}: mode {mode!r} uses unknown profiles {unknown}")
    return model
