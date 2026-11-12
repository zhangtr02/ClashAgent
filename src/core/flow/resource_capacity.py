"""Read game-provided capacities without guessing or starting a battle."""
from core.errors import ClashAgentError
from core.flow.models import FlowEvent
from core.resources import RESOURCE_LABELS


class CapacityScanner:
    TIMEOUT = 8.0

    def __init__(self, reader, control, tap, notify):
        self.reader, self.control = reader, control
        self.tap, self.notify = tap, notify
        self.values = {}
        self.opened = None
        self.closing = None
        self.started = None
        self.previous = None
        self.stable = 0
        self.version = None

    def tick(self, frame, resources, version):
        now = self.control.active_time()
        if version != self.version:
            self.version = version
            self.previous, self.stable = None, 0
        popup = self.reader.capacity_popup(frame)
        visible = popup[0] if popup else None
        if self.closing is not None:
            if visible is None:
                self.closing = self.opened = self.started = None
            else:
                if visible != self.closing:
                    raise ClashAgentError("容量说明在关闭时发生变化，请停止后重新读取。")
                if now-self.started >= self.TIMEOUT:
                    raise ClashAgentError("资源容量说明未能关闭，未开始搜索。")
                return None
        target = next((resource for resource in resources if resource not in self.values), None)
        if visible is not None and (self.opened is None or visible != target):
            # Dismiss a tooltip already open before the run. Its own HUD bar
            # lies above the tooltip and cannot activate a purchase button.
            if self.opened is not None:
                raise ClashAgentError("打开的容量说明与目标资源不一致，未开始搜索。")
            if self.tap(version, *self.reader.resource_target(visible)):
                self.closing, self.started = visible, now
            return None
        if target is None:
            return dict(self.values)
        if self.opened is None:
            if self.tap(version, *self.reader.resource_target(target)):
                self.opened, self.started = target, now
                self.previous, self.stable = None, 0
            return None
        if now-self.started >= self.TIMEOUT:
            raise ClashAgentError(f"无法可靠识别{RESOURCE_LABELS[target]}最大储量，已暂停且未搜索。")
        if visible != target:
            return None  # Never blindly toggle a still-opening tooltip.
        result = self.reader.read_capacity(frame, target)
        if result is None:
            self.previous, self.stable = None, 0
            return None
        capacity, confidence = result
        self.stable = self.stable+1 if capacity == self.previous else 1
        self.previous = capacity
        if self.stable < 2:
            return None
        self.values[target] = capacity
        self.notify(target, capacity, confidence)
        if self.tap(version, *self.reader.resource_target(target)):
            self.closing, self.started = target, now
        return None


def read_capacities(reader, device, control, recognizer, emit, *, builder=False):
    """Explicit button task only; this module never enters the farming loop."""
    frame = None
    resources = ("gold",) if builder else RESOURCE_LABELS
    home = "builder_base" if builder else "village"
    home_label = "建筑大师基地" if builder else "主世界村庄"

    def tap(version, x, y):
        device.ensure_foreground()
        return control.dispatch(version, lambda: device.tap(
            round(x*frame.width/1280), round(y*frame.height/720)))

    def notify(resource, capacity, confidence):
        emit(FlowEvent("capacity_verified", "reading_capacity", "capacity_read",
                       f"{RESOURCE_LABELS[resource]}最大容量：{capacity:,}", frame,
                       details={"resource": resource, "capacity": capacity, "confidence": confidence}))

    scanner = CapacityScanner(reader, control, tap, notify)
    while True:
        version = control.checkpoint(lambda _: None)
        frame = device.capture()
        if control.checkpoint(lambda _: None) != version:
            continue
        state = recognizer.recognize(frame)
        emit(FlowEvent("observation", "reading_capacity", "capacity_read", f"读取{home_label}资源说明", frame, state))
        if state.state != home:
            raise ClashAgentError(f"请进入{home_label}后读取最大容量。")
        resolved = scanner.tick(frame, resources, version)
        if resolved is not None:
            return resolved
        control.wait(.12)
