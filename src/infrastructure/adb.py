import os
import shutil
from pathlib import Path

from core.errors import ClashAgentError
from infrastructure.adb_server import AdbServer
from infrastructure.settings import LOCAL_ADB_PATH, PROJECT_ROOT
from infrastructure.windows_runtime import WindowsRuntime


def _find_mumu_adb(serial: str) -> str | None:
    if os.name != "nt":
        return None
    runtime = WindowsRuntime()
    try:
        processes = runtime.processes()
    except ClashAgentError:
        return None

    owners = set()
    _, _, port = serial.rpartition(":")
    if len(processes) > 1 and port.isascii() and port.isdecimal():
        try:
            owners = {row.pid for row in runtime.listeners() if row.port == int(port)}
        except ClashAgentError:
            pass
    for process in sorted(processes, key=lambda item: (item.pid not in owners, item.pid)):
        image = runtime.executable_path(process.pid)
        if image is None or not image.is_absolute() or image.name.lower() != process.name.lower():
            continue
        directories = [image.parent]
        for parent in image.parents:
            if parent.name.lower() == "nx_device":
                directories.append(parent.parent / "nx_main")
                break
            if parent.name.lower() == "nemu":
                directories.append(parent / "vmonitor" / "bin")
                break
        for directory in directories:
            for name in ("adb.exe", "adb_server.exe"):
                candidate = directory / name
                if candidate.is_file():
                    return str(candidate)
    return None


def _find_adb(executable: str, serial: str) -> str | None:
    if executable := executable.strip():
        candidate = PROJECT_ROOT / Path(executable).expanduser()
        found = str(candidate.resolve()) if candidate.is_file() else shutil.which(executable)
        if not found:
            raise ClashAgentError("找不到指定的 ADB 程序。请重新选择，或清空路径以自动查找。")
        return found

    if found := _find_mumu_adb(serial) or shutil.which("adb.exe"):
        return found

    sdk_roots = [Path(value) for name in ("ANDROID_HOME", "ANDROID_SDK_ROOT")
                 if (value := os.environ.get(name))]
    if local_app_data := os.environ.get("LOCALAPPDATA"):
        sdk_roots.append(Path(local_app_data) / "Android" / "Sdk")
    candidates = [root / "platform-tools" / "adb.exe" for root in sdk_roots]
    candidates.append(PROJECT_ROOT / LOCAL_ADB_PATH)
    return next((str(path.resolve()) for path in candidates if path.is_file()), None)


class AdbClient:
    """Reuse the local ADB server and start it with an available tool if needed."""

    def __init__(self, executable: str, serial: str):
        self.executable = _find_adb(executable, serial)
        self.serial = serial
        self.server = AdbServer(self.executable)

    def device_states(self) -> dict[str, str]:
        output = self.server.host("host:devices-l")
        return {parts[0]: parts[1] for line in output.splitlines() if len(parts := line.split()) >= 2}

    def connect(self) -> None:
        output = self.server.host("host:connect:" + self.serial)
        if any(word in output.lower() for word in ("cannot", "unable", "failed", "refused")):
            raise ClashAgentError(f"MuMu 连接失败：{output.strip()}")
        self.require_online()

    def require_online(self) -> None:
        state = self.device_states().get(self.serial)
        if state == "device":
            return
        if state == "unauthorized":
            raise ClashAgentError("设备未授权 ADB，请在 MuMu 中确认调试授权后重试。")
        if state == "offline":
            raise ClashAgentError("MuMu ADB 设备处于 offline 状态，请等待恢复或重新连接。")
        raise ClashAgentError(f"设备 {self.serial} 未连接。请先打开 MuMu 或重新发现实例。")

    def shell(self, *args: str, timeout: float = 15) -> str:
        return self.server.shell(self.serial, args, timeout)

    def screenshot(self) -> bytes:
        return self.server.screenshot(self.serial)

    def screenshot_raw(self) -> bytes:
        return self.server.exec(self.serial, "screencap")

    def tap(self, x: int, y: int) -> None:
        if type(x) is not int or type(y) is not int or x < 0 or y < 0:
            raise ClashAgentError("点击坐标必须为非负整数。")
        self.shell("input", "tap", str(x), str(y), timeout=5)

    def disconnect(self) -> None:
        self.server.host("host:disconnect:" + self.serial)
