"""Use a validated preferred backend, with latency-based fallbacks."""

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from statistics import median
from threading import RLock
from time import perf_counter

from core.capture import ScreenFrame
from core.errors import ClashAgentError


@dataclass(frozen=True)
class BackendMeasurement:
    name: str
    durations_ms: tuple[float, ...] = ()
    error: str = ""
    warmup_ms: float = 0

    @property
    def median_ms(self) -> float:
        return median(self.durations_ms) if self.durations_ms else float("inf")


@dataclass(frozen=True)
class CaptureReport:
    measurements: tuple[BackendMeasurement, ...]
    selected: str
    measured_at: datetime


@dataclass(frozen=True)
class CaptureResult:
    frame: ScreenFrame
    report: CaptureReport


class CaptureManager:
    def __init__(self, backends: dict[str, Callable[[], ScreenFrame]], *,
                 preferred: str = "",
                 warmups: set[str] | None = None,
                 validators: dict[str, Callable[[ScreenFrame, ScreenFrame | None], None]] | None = None,
                 closers: tuple[Callable[[], None], ...] = ()):
        if not backends:
            raise ValueError("At least one capture backend is required")
        self.backends = dict(backends)
        self.preferred = preferred
        self.report: CaptureReport | None = None
        self._lock = RLock()
        self.warmups = warmups or set()
        self.validators = validators or {}
        self._closers = list(closers)
        self._closed = False

    @contextmanager
    def _operation(self):
        with self._lock:
            if self._closed:
                raise ClashAgentError("截图会话已关闭。")
            try:
                yield
            finally:
                if self._closed:
                    self._dispose()

    def _dispose(self) -> None:
        while self._closers:
            self._closers.pop()()

    def close(self) -> None:
        self._closed = True
        # Do not block the UI on an in-flight capture. Its finally block owns
        # resource cleanup; the generation guard discards its late result.
        if self._lock.acquire(blocking=False):
            try:
                self._dispose()
            finally:
                self._lock.release()

    def _capture(self, name: str) -> ScreenFrame:
        if self._closed:
            raise ClashAgentError("截图会话已关闭。")
        start = perf_counter()
        frame = self.backends[name]()
        return replace(frame, elapsed_ms=(perf_counter() - start) * 1000, backend=name)

    def benchmark(self, samples: int = 3) -> CaptureReport:
        if not 1 <= samples <= 10:
            raise ValueError("Benchmark sample count must be between 1 and 10")
        with self._operation():
            measurements = []
            reference = None
            for name in self.backends:
                durations = []
                error = ""
                warmup_ms = 0
                try:
                    if name in self.warmups:
                        warmup_ms = self._capture(name).elapsed_ms
                    for _ in range(samples):
                        frame = self._capture(name)
                        if name in self.validators:
                            self.validators[name](frame, reference)
                        elif reference is None:
                            reference = frame
                        durations.append(frame.elapsed_ms)
                except Exception as exc:
                    if self._closed:
                        raise ClashAgentError("截图会话已关闭。") from exc
                    error = str(exc) or type(exc).__name__
                measurements.append(BackendMeasurement(name, tuple(durations), error, warmup_ms))
            valid = [item for item in measurements if not item.error and item.durations_ms]
            selected = min(valid, key=lambda item: (item.name != self.preferred, item.median_ms)).name if valid else ""
            self.report = CaptureReport(tuple(measurements), selected, datetime.now().astimezone())
            if not selected:
                raise ClashAgentError("所有截图后端均不可用：" + "；".join(f"{item.name}: {item.error}" for item in measurements))
            return self.report

    def capture(self, mode: str = "auto") -> CaptureResult:
        with self._operation():
            if mode != "auto":
                if mode not in self.backends:
                    raise ClashAgentError(f"未知截图后端：{mode}")
                if mode in self.validators:
                    if self.report is None:
                        self.benchmark(samples=2)
                    item = next(item for item in self.report.measurements if item.name == mode)
                    if item.error:
                        raise ClashAgentError(f"截图后端未通过校验：{item.error}")
                frame = self._capture(mode)
                report = self.report or CaptureReport((), mode, datetime.now().astimezone())
                return CaptureResult(frame, report)
            if self.report is None:
                self.benchmark(samples=2)
            while self.report.selected:
                name = self.report.selected
                try:
                    return CaptureResult(self._capture(name), self.report)
                except Exception as exc:
                    measurements = tuple(replace(item, error=str(exc) or type(exc).__name__) if item.name == name else item
                                         for item in self.report.measurements)
                    valid = [item for item in measurements if not item.error and item.durations_ms]
                    selected = min(valid, key=lambda item: item.median_ms).name if valid else ""
                    self.report = replace(self.report, measurements=measurements, selected=selected)
            raise ClashAgentError("截图后端全部失效，请重新连接或重新测速。")
