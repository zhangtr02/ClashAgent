from dataclasses import dataclass, field
from functools import cached_property
from threading import RLock
from datetime import datetime
from time import perf_counter
from typing import Protocol
from uuid import uuid4

import cv2
import numpy as np

from core.errors import ClashAgentError


def decode_png(png: bytes) -> np.ndarray:
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ClashAgentError("截图不是有效 PNG，请检查 ADB 截图输出。")
    if len(png) < 33 or png[8:16] != b"\x00\x00\x00\x0dIHDR":
        raise ClashAgentError("PNG 图像头已损坏。")
    width, height = int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")
    if width < 1 or height < 1 or width * height > 64_000_000:
        raise ClashAgentError("PNG 图像尺寸无效或超过 6400 万像素。")
    try:
        image = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)
    except cv2.error as exc:
        raise ClashAgentError(f"截图解码失败：{exc}") from exc
    if image is None or image.size == 0:
        raise ClashAgentError("PNG 图像为空或已损坏。")
    return image


@dataclass(frozen=True)
class ScreenFrame:
    image: np.ndarray = field(repr=False, compare=False)
    captured_at: datetime
    source: str
    elapsed_ms: float = 0
    backend: str = "png"
    frame_id: str = field(default_factory=lambda: uuid4().hex)
    _original_png: bytes | None = field(default=None, repr=False, compare=False)
    _cache_lock: RLock = field(default_factory=RLock, repr=False, compare=False)

    @property
    def width(self) -> int:
        return self.image.shape[1]

    @property
    def height(self) -> int:
        return self.image.shape[0]

    @cached_property
    def png(self) -> bytes:
        with self._cache_lock:
            existing = self.__dict__.get("png")
            if existing is not None:
                return existing
            if self._original_png is not None:
                return self._original_png
            ok, encoded = cv2.imencode(".png", self.image)
            if not ok:
                raise ClashAgentError("无法将画面导出为 PNG。")
            result = encoded.tobytes()
            object.__setattr__(self, "png", result)
            return result

    @cached_property
    def gray(self) -> np.ndarray:
        with self._cache_lock:
            existing = self.__dict__.get("gray")
            if existing is not None:
                return existing
            image = cv2.cvtColor(self.image, cv2.COLOR_BGR2GRAY)
            image.setflags(write=False)
            object.__setattr__(self, "gray", image)
            return image

    @classmethod
    def from_png(cls, png: bytes, source: str, elapsed_ms: float = 0, backend: str = "png") -> "ScreenFrame":
        image = decode_png(png)
        image.setflags(write=False)
        return cls(image, datetime.now().astimezone(), source, elapsed_ms, backend, _original_png=png)

    @classmethod
    def from_bgr(cls, image: np.ndarray, source: str, elapsed_ms: float = 0, backend: str = "raw") -> "ScreenFrame":
        if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3 or image.size == 0:
            raise ClashAgentError("画面必须为非空 uint8 BGR 图像。")
        if image.shape[0] * image.shape[1] > 64_000_000:
            raise ClashAgentError("画面超过 6400 万像素。")
        owned = np.array(image, copy=True, order="C")
        owned.setflags(write=False)
        return cls(owned, datetime.now().astimezone(), source, elapsed_ms, backend)


class ScreenshotSource(Protocol):
    def screenshot(self) -> bytes: ...


class CaptureService:
    def __init__(self, device: ScreenshotSource, source: str):
        self.device = device
        self.source = source

    def capture(self) -> ScreenFrame:
        start = perf_counter()
        png = self.device.screenshot()
        frame = ScreenFrame.from_png(png, self.source)
        object.__setattr__(frame, "elapsed_ms", (perf_counter() - start) * 1000)
        return frame
