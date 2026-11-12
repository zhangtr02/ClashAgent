import json
from dataclasses import dataclass, field
from pathlib import Path

from core.errors import ClashAgentError
from core.resources import FarmOptions, RESOURCE_LABELS, ResourceGoal
from infrastructure.settings import PROJECT_ROOT


@dataclass(frozen=True)
class FarmPreferences:
    percent: int = 90
    capacities: dict[str, int] = field(default_factory=lambda: {name: 0 for name in RESOURCE_LABELS})
    enabled: tuple[str, ...] = ()

    def options(self) -> FarmOptions:
        if type(self.percent) is not int or not 10 <= self.percent <= 100:
            raise ClashAgentError("资源停止比例应为 10%–100%。")
        if (set(self.capacities) != set(RESOURCE_LABELS) or
                any(type(v) is not int or not 0 <= v <= 2_000_000_000 for v in self.capacities.values()) or
                len(self.enabled) != len(set(self.enabled)) or any(name not in RESOURCE_LABELS for name in self.enabled)):
            raise ClashAgentError("资源容量配置不合法。")
        # Missing cache disables only that selected condition. Never open a
        # capacity tooltip from the farming loop or guess a storage size.
        options = FarmOptions(tuple(ResourceGoal(name, self.capacities[name], self.percent)
                                    for name in self.enabled if self.capacities[name] > 0))
        options.validate()
        return options


class FarmSettingsStore:
    def __init__(self, path: Path | None = None):
        data_dir = PROJECT_ROOT / "data"
        self.path = path or data_dir / "village_farm.json"
        self.legacy_path = data_dir / "farm.json" if path is None else None

    def load(self) -> FarmPreferences:
        path = self.path
        if not path.exists() and self.legacy_path is not None and self.legacy_path.is_file():
            try:
                self.legacy_path.rename(path)
            except OSError:
                if not path.exists():
                    path = self.legacy_path
        if not path.exists():
            return FarmPreferences()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("version") != 1:
                raise ValueError("不支持的刷资源配置版本")
            preferences = FarmPreferences(data["percent"], data["capacities"], tuple(data["enabled"]))
            preferences.options()
            return preferences
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ClashAgentError(f"刷资源配置读取失败：{exc}") from exc

    def save(self, preferences: FarmPreferences) -> None:
        preferences.options()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = {"version": 1, "percent": preferences.percent, "capacities": preferences.capacities,
                    "enabled": list(preferences.enabled)}
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
            temporary.replace(self.path)
        except OSError as exc:
            raise ClashAgentError(f"刷资源配置保存失败：{exc}") from exc
