from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class MuMuInstance:
    key: str
    pid: int
    name: str
    endpoints: tuple[str, ...]

    def endpoint(self, preferred: str = "") -> str:
        if preferred in self.endpoints:
            return preferred
        return self.endpoints[0] if self.endpoints else ""


@dataclass(frozen=True)
class DiscoverySnapshot:
    instances: tuple[MuMuInstance, ...]
    device_states: dict[str, str]
    observed_at: datetime
    discovery_error: str = ""
    adb_error: str = ""


def select_instance(instances: tuple[MuMuInstance, ...], selected_key: str | None,
                    allow_single_replacement: bool = False) -> MuMuInstance | None:
    selected = next((instance for instance in instances if instance.key == selected_key), None)
    if selected:
        return selected
    if len(instances) == 1 and (selected_key is None or allow_single_replacement):
        return instances[0]
    return None
