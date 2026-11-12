import struct
from collections.abc import Callable

import cv2
import numpy as np

from core.capture import ScreenFrame
from core.capture_manager import CaptureManager
from core.errors import ClashAgentError
from infrastructure.adb import AdbClient
from infrastructure.settings import Settings
from infrastructure.mumu_native import MuMuNativeCapture


def decode_android_raw(data: bytes) -> np.ndarray:
    if len(data) < 12:
        raise ClashAgentError("ADB Raw 图像头不完整。")
    width, height, pixel_format = struct.unpack_from("<III", data)
    channels = {1: 4, 2: 4, 3: 3, 4: 2, 5: 4}.get(pixel_format)
    if not channels or width < 1 or height < 1 or width * height > 64_000_000:
        raise ClashAgentError(f"不支持的 Raw 图像格式或尺寸：{width}×{height}, format={pixel_format}")
    expected = width * height * channels
    header_size = len(data) - expected
    if header_size not in (12, 16):
        raise ClashAgentError("Raw 图像长度与尺寸不匹配，拒绝使用截断或未知布局的数据。")
    image = np.frombuffer(data, dtype=np.uint8, offset=header_size).reshape(height, width, channels)
    conversion = {1: cv2.COLOR_RGBA2BGR, 2: cv2.COLOR_RGBA2BGR, 3: cv2.COLOR_RGB2BGR,
                  4: cv2.COLOR_BGR5652BGR, 5: cv2.COLOR_BGRA2BGR}[pixel_format]
    return cv2.cvtColor(image, conversion)


def build_capture_manager(settings: Settings, client_factory: Callable = AdbClient) -> CaptureManager:
    client = None

    def device():
        nonlocal client
        if client is None:
            client = client_factory(settings.adb_path, settings.endpoint)
        return client

    def raw():
        method = getattr(device(), "screenshot_raw", None)
        if method is None:
            raise ClashAgentError("设备未提供 Raw 截图能力。")
        return ScreenFrame.from_bgr(decode_android_raw(method()), settings.endpoint, backend="adb_raw")

    def png():
        return ScreenFrame.from_png(device().screenshot(), settings.endpoint, backend="adb_png")

    native = MuMuNativeCapture(settings)
    def validate_native(frame, reference):
        try:
            validate_native_frame(frame, reference)
        except Exception:
            native.close()
            raise
    return CaptureManager({"adb_raw": raw, "adb_png": png, "mumu_native": native.capture},
                          preferred="mumu_native",
                          warmups={"mumu_native"}, validators={"mumu_native": validate_native},
                          closers=(native.close,))


def validate_native_frame(frame: ScreenFrame, reference: ScreenFrame | None) -> None:
    if reference is None:
        raise ClashAgentError("没有 ADB 参考画面，不能校验 MuMu 原生画面的实例与方向。")
    if (frame.width, frame.height) != (reference.width, reference.height):
        raise ClashAgentError("MuMu 原生截图尺寸与 ADB 不一致，拒绝自动切换。")
    small = cv2.resize(frame.gray, (96, 54), interpolation=cv2.INTER_AREA).astype(np.int16)
    other = cv2.resize(reference.gray, (96, 54), interpolation=cv2.INTER_AREA).astype(np.int16)
    difference = np.abs(small - other)
    if float(difference.mean()) > 25 or float(np.mean(difference < 40)) < 0.75:
        raise ClashAgentError("MuMu 原生截图与 ADB 参考画面不一致；请保持画面稳定后重新测速。")
