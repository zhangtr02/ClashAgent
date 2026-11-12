from collections.abc import Callable
from threading import Condition
from time import monotonic


class FlowStopped(Exception):
    def __init__(self, reason: str = ""):
        self.reason = reason


class RunControl:
    """Pause acknowledgement and a generation gate before every input."""

    def __init__(self):
        self._condition = Condition()
        self._paused = False
        self._stopped = False
        self._reason = ""
        self._pause_reason = ""
        self._version = 0
        self._paused_at = 0.0
        self._paused_total = 0.0

    def pause(self, reason: str = "用户暂停") -> None:
        with self._condition:
            if not self._paused and not self._stopped:
                self._paused = True
                self._paused_at = monotonic()
                self._pause_reason = reason
                self._version += 1
            self._condition.notify_all()

    def resume(self) -> None:
        with self._condition:
            if self._paused and not self._stopped:
                self._paused_total += monotonic() - self._paused_at
                self._paused = False
                self._version += 1
            self._condition.notify_all()

    def stop(self, reason: str = "") -> None:
        with self._condition:
            self._stopped = True
            self._reason = self._reason or reason
            self._version += 1
            self._condition.notify_all()

    def active_time(self) -> float:
        with self._condition:
            now = monotonic()
            return now - self._paused_total - (now - self._paused_at if self._paused else 0)

    def checkpoint(self, on_paused: Callable[[str], None]) -> int:
        with self._condition:
            notified_version = None
            while self._paused and not self._stopped:
                if notified_version != self._version:
                    notified_version = self._version
                    on_paused(self._pause_reason)
                self._condition.wait(0.1)
            if self._stopped:
                raise FlowStopped(self._reason)
            return self._version

    def dispatch(self, expected_version: int, action: Callable[[], None]) -> bool:
        with self._condition:
            if self._stopped:
                raise FlowStopped(self._reason)
            if self._paused or expected_version != self._version:
                return False
            # This is the input's acceptance point. Pause/stop can still be
            # requested while the already accepted network operation finishes.
        action()
        return True

    def wait(self, seconds: float) -> None:
        with self._condition:
            if not self._paused and not self._stopped:
                self._condition.wait(seconds)
