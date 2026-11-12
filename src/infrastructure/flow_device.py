import re

from core.capture_manager import CaptureManager
from core.errors import ClashAgentError
from infrastructure.adb import AdbClient
from infrastructure.settings import Settings
from infrastructure.touch import TouchChannel


class AndroidFlowDevice:
    def __init__(self, settings: Settings, capture: CaptureManager):
        self.settings = settings
        self.capture_manager = capture
        self.client = AdbClient(settings.adb_path, settings.endpoint)
        self.touch = TouchChannel(self.client)

    def capture(self):
        return self.capture_manager.capture().frame

    def tap(self, x: int, y: int) -> None:
        self.client.tap(x, y)

    def fast_tap(self, x, y, frame, control, version):
        # Deployment checks foreground once before each bounded input batch.
        return self.touch.tap(x, y, frame.width, frame.height, control, version)

    def gesture(self, paths, frame, control, version):
        self.ensure_foreground()
        return self.touch.gesture(paths, frame.width, frame.height, control, version)

    def close(self):
        self.touch.close()

    def ensure_foreground(self) -> None:
        output = self.client.shell("dumpsys", "activity", "activities", timeout=5)
        pattern = r"(?<![A-Za-z0-9_.])" + re.escape(self.settings.package) + "/"
        if not any(("ResumedActivity" in line or "topResumedActivity" in line) and re.search(pattern, line)
                   for line in output.splitlines()):
            raise ClashAgentError("目标游戏不在前台，已阻止输入；请切回游戏后重新开始任务。")
