from dataclasses import dataclass


@dataclass(frozen=True)
class DeviceInfo:
    serial: str
    model: str
    android_version: str
    resolution: str


@dataclass(frozen=True)
class LaunchResult:
    package: str
    activity: str
    foreground_confirmed: bool
