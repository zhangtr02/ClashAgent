import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from core.errors import ClashAgentError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "data" / "settings.json"
LOCAL_ADB_PATH = Path("3rdparty/platform-tools/adb.exe")


@dataclass(frozen=True)
class Settings:
    adb_path: str = ""
    endpoint: str = ""
    package: str = "com.supercell.clashofclans"
    connection_mode: str = "auto"

    def validate(self) -> None:
        if self.connection_mode not in ("auto", "manual"):
            raise ClashAgentError("连接模式应为 auto 或 manual。")
        if self.endpoint or self.connection_mode == "manual":
            match = re.fullmatch(r"(127\.0\.0\.1|localhost):([0-9]{1,5})", self.endpoint)
            if not match or not 1 <= int(match[2]) <= 65535:
                raise ClashAgentError("MuMu 地址应为 127.0.0.1:端口，端口范围为 1–65535。")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", self.package):
            raise ClashAgentError("游戏包名格式不正确。")


class SettingsStore:
    def __init__(self, path: Path = DEFAULT_SETTINGS_PATH):
        self.path = path

    def load(self) -> Settings:
        if not self.path.exists():
            return Settings()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("配置必须为 JSON 对象")
            defaults = asdict(Settings())
            values = {key: data.get(key, value) for key, value in defaults.items()}
            if not all(isinstance(value, str) for value in values.values()):
                raise ValueError("配置字段必须为字符串")
            if Path(values["adb_path"]) == LOCAL_ADB_PATH:
                values["adb_path"] = ""
            settings = Settings(**values)
            settings.validate()
            return settings
        except (OSError, ValueError, TypeError) as exc:
            raise ClashAgentError(f"无法读取配置 {self.path}：{exc}") from exc

    def save(self, settings: Settings) -> None:
        settings.validate()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.path)
        except OSError as exc:
            raise ClashAgentError(f"无法保存配置：{exc}") from exc
