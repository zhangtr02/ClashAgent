from collections.abc import Callable
from typing import Protocol
from uuid import uuid4

from core.capture import ScreenFrame
from core.flow.control import FlowStopped, RunControl
from core.flow.models import FlowDefinition, FlowEvent, FlowResult
from core.flow.recognizer import StateRecognizer
from core.resources import ResourceTargetReached


class FlowDevice(Protocol):
    def capture(self) -> ScreenFrame: ...
    def ensure_foreground(self) -> None: ...
    def tap(self, x: int, y: int) -> None: ...


class TaskRunner:
    def __init__(self, definition: FlowDefinition, device: FlowDevice, control: RunControl,
                 emit: Callable[[FlowEvent], None], recognizer=None, run_id: str | None = None,
                 before_village=None, *, home_state="village"):
        self.definition, self.device, self.control = definition, device, control
        self.emit = emit
        self.recognizer = recognizer or StateRecognizer(definition)
        self.run_id = run_id or uuid4().hex
        self.phase, self.node = "starting", ""
        self.last_frame = None
        self.message = ""
        self.before_village = before_village
        self.home_state = home_state

    def event(self, kind: str, message: str = "", recognition=None, frame=None) -> None:
        if kind == "terminal":
            self.message = message
        self.emit(FlowEvent(kind, self.phase, self.node, message, frame, recognition, self.run_id))

    def checkpoint(self) -> int:
        def paused(reason):
            self.phase = "paused"
            self.event("phase", reason, frame=self.last_frame)
        version = self.control.checkpoint(paused)
        if self.phase in ("starting", "paused"):
            self.phase = "running"
            self.event("phase", "重新截图确认状态" if self.last_frame else "准备画面与识别资源")
        return version

    def run(self) -> FlowResult:
        self.event("phase", "任务准备中")
        allowed = set(self.definition.start)
        pending = None
        attempts = {}
        actions = 0
        last_action_at = 0
        stable_name, stable_count = "", 0
        last_version = None
        unknown_since = self.control.active_time()
        try:
            while True:
                version = self.checkpoint()
                if version != last_version:
                    stable_name, stable_count = "", 0
                    unknown_since = self.control.active_time()
                    attempts.clear()
                    # An explicit resume re-enters from a freshly recognized
                    # start candidate, including after manual UI recovery.
                    pending = None
                    allowed = set(self.definition.start)
                    last_version = version
                self.device.ensure_foreground()
                frame = self.device.capture()
                self.last_frame = frame
                if self.checkpoint() != version:
                    continue
                recognition = self.recognizer.recognize(frame)
                self.event("observation", recognition.label, recognition, frame)
                now = self.control.active_time()
                if not recognition.state:
                    stable_name, stable_count = "", 0
                    timeout = self.definition.node(pending[0]).timeout if pending else self.definition.unknown_timeout
                    origin = pending[1] if pending else unknown_since
                    if now - origin >= timeout:
                        self.event("warning", "界面识别超时，等待人工检查", recognition, frame)
                        self.control.pause("界面识别超时；请检查游戏画面后继续")
                    self.control.wait(self.definition.poll_interval)
                    continue
                stable_count = stable_count + 1 if stable_name == recognition.state else 1
                stable_name = recognition.state
                if stable_count < self.definition.stable_frames:
                    self.control.wait(self.definition.poll_interval)
                    continue
                node = self.definition.node(recognition.state)
                if node.name not in allowed and node.action != "pause":
                    self.control.pause(f"识别到不在当前流程后续中的界面：{node.label}")
                    continue
                unknown_since = now
                if self.node != node.name:
                    self.node = node.name
                    self.event("state", node.label, recognition, frame)
                if pending and pending[0] != node.name:
                    self.event("verified", f"已验证进入：{node.label}", recognition, frame)
                    pending = None
                if node.name == self.home_state and self.before_village and not self.before_village(frame, version):
                    self.control.wait(self.definition.poll_interval)
                    continue
                if node.action == "complete":
                    self.phase = "completed"
                    self.event("terminal", node.message or node.label, recognition, frame)
                    break
                if node.action == "pause":
                    self.control.pause(node.message or node.label)
                    continue
                if node.action == "wait":
                    if pending is None:
                        pending = (node.name, now)
                        allowed = {node.name, *node.next}
                    elif now - pending[1] >= node.timeout:
                        self.control.pause(f"等待 {node.label} 完成超时")
                    self.control.wait(self.definition.poll_interval)
                    continue
                if pending:
                    elapsed = now - pending[1]
                    if elapsed >= node.timeout:
                        self.control.pause(f"点击后未验证进入下一界面：{node.label}")
                        continue
                    if now - last_action_at < node.retry_interval or attempts.get(node.name, 0) >= node.max_attempts:
                        self.control.wait(self.definition.poll_interval)
                        continue
                if actions >= self.definition.max_actions:
                    raise RuntimeError("本次任务达到动作上限，已阻止继续输入")
                hit = next(hit for hit in recognition.hits if hit.name == node.target)
                x = hit.region.x + hit.region.width // 2
                y = hit.region.y + hit.region.height // 2
                if not 0 <= x < frame.width or not 0 <= y < frame.height:
                    raise RuntimeError("识别到的点击目标超出截图范围")
                self.device.ensure_foreground()
                if not self.control.dispatch(version, lambda: self.device.tap(x, y)):
                    continue
                actions += 1
                last_action_at = now
                attempts[node.name] = attempts.get(node.name, 0) + 1
                # A successful transport acknowledgement is not a UI transition.
                # Keep waiting for a fresh, stable next state before advancing.
                if pending is None:
                    pending = (node.name, self.control.active_time())
                allowed = {node.name, *node.next}
                stable_name, stable_count = "", 0
                self.event("action", f"{node.label}：点击 {node.target} ({x}, {y})，等待后续界面验证", recognition, frame)
                self.control.wait(self.definition.poll_interval)
        except ResourceTargetReached:
            raise
        except FlowStopped as exc:
            self.phase = "failed" if exc.reason else "stopped"
            self.event("terminal", exc.reason or "用户停止；未发送后续动作", frame=self.last_frame)
        except Exception as exc:
            self.phase = "failed"
            self.event("terminal", str(exc) or type(exc).__name__, frame=self.last_frame)
        return FlowResult(self.phase, self.node, self.message, self.run_id)
