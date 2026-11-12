import re
import time
from collections.abc import Callable

from core.errors import ClashAgentError
from core.models import DeviceInfo, LaunchResult
from core.ports import AndroidDevice


class DeviceService:
    def __init__(self, device: AndroidDevice, serial: str,
                 report: Callable[[str], None] = lambda _: None):
        self.device = device
        self.serial = serial
        self.report = report

    def connect(self) -> DeviceInfo:
        self.report(f"连接已打开的 MuMu：{self.serial}")
        self.device.connect()
        return self.inspect()

    def inspect(self) -> DeviceInfo:
        self.device.require_online()
        # ADB online does not necessarily mean Android has finished booting.
        if self.device.shell("getprop", "sys.boot_completed").strip() != "1":
            raise ClashAgentError("MuMu 的 Android 系统尚未启动完成，请等待进入桌面后重试。")
        model = self.device.shell("getprop", "ro.product.model").strip()
        version = self.device.shell("getprop", "ro.build.version.release").strip()
        size = self.device.shell("wm", "size").strip()
        self.report("设备已连接，Android 已就绪。")
        return DeviceInfo(self.serial, model or "未知", version or "未知", size or "未知")

    def disconnect(self) -> None:
        self.device.disconnect()
        self.report("已断开当前 ADB 连接；MuMu 和游戏继续运行。")

    def launch_game(self, package: str, foreground_timeout: float = 12) -> LaunchResult:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", package):
            raise ClashAgentError("游戏包名格式不正确。")
        self.device.require_online()
        self.report(f"检查游戏安装状态：{package}")
        installed = self.device.shell("pm", "path", package)
        if not any(line.startswith("package:") for line in installed.splitlines()):
            raise ClashAgentError(f"当前设备未安装 {package}。请核对实例与游戏包名。")

        output = self.device.shell(
            "cmd", "package", "resolve-activity", "--brief",
            "-a", "android.intent.action.MAIN",
            "-c", "android.intent.category.LAUNCHER", package,
        )
        # Resolve the launcher rather than hard-coding a game activity name.
        candidates = [line.strip() for line in output.splitlines()]
        activity = next((line for line in reversed(candidates)
                         if re.fullmatch(re.escape(package) + r"/[A-Za-z0-9_.]+", line)), None)
        if activity is None:
            raise ClashAgentError("无法解析游戏启动 Activity，请检查该版本是否提供桌面启动入口。")
        self.report(f"启动游戏：{activity}")
        started = self.device.shell("am", "start", "-W", "-n", activity, timeout=30)
        if re.search(r"(?im)^\s*(error(?:\s|:)|exception|status:\s*(?!ok\b)\S+)", started):
            raise ClashAgentError(f"Android 未能启动游戏：\n{started.strip()}")

        deadline = time.monotonic() + foreground_timeout
        while True:
            resumed = self.device.shell("dumpsys", "activity", "activities", timeout=10)
            if any(("ResumedActivity" in line or "topResumedActivity" in line)
                   and re.search(r"(?<![A-Za-z0-9_.])" + re.escape(package) + r"/", line)
                   for line in resumed.splitlines()):
                self.report("已确认游戏 Activity 位于前台；游戏加载与登录状态需查看模拟器。")
                return LaunchResult(package, activity, True)
            if time.monotonic() >= deadline:
                raise ClashAgentError("已发送启动指令，但未确认游戏进入前台。请检查 MuMu 是否有系统弹窗或游戏启动异常。")
            time.sleep(0.5)
