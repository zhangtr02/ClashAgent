"""Vendor SDK capture isolated from Qt in a bounded, disposable worker process."""

import ctypes
import json
import multiprocessing
import os
import re
import subprocess
from dataclasses import dataclass
from multiprocessing import shared_memory
from pathlib import Path

import cv2
import numpy as np

from core.capture import ScreenFrame
from core.errors import ClashAgentError
from infrastructure.settings import Settings
from infrastructure.windows_runtime import WindowsRuntime

_CAPACITY = 64 * 1024 * 1024


@dataclass(frozen=True)
class NativeBinding:
    pid: int
    port: int
    root: str
    index: int
    dll: str


def resolve_binding(settings: Settings) -> NativeBinding:
    if os.name != "nt":
        raise ClashAgentError("MuMu 原生截图仅支持 Windows。")
    if not settings.endpoint:
        raise ClashAgentError("尚未解析 MuMu 实例地址。")
    port = int(settings.endpoint.rsplit(":", 1)[1])
    runtime = WindowsRuntime()
    processes = {item.pid: item for item in runtime.processes()}
    owners = {item.pid for item in runtime.listeners() if item.port == port and item.pid in processes}
    if len(owners) != 1:
        raise ClashAgentError("无法将当前 ADB 地址唯一绑定到运行中的 MuMu 实例。")
    pid = owners.pop()
    powershell = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    command = ("[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
               f"Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}' | "
               "Select-Object ExecutablePath,CommandLine | ConvertTo-Json -Compress")
    try:
        result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-Command", command],
                                capture_output=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
        info = json.loads(result.stdout.decode("utf-8-sig"))
        path = Path(info["ExecutablePath"])
        match = re.search(r'--vm\s+"?(MuMuPlayer-[0-9.]+-([0-9]+))(?=["\s]|$)', info["CommandLine"], re.IGNORECASE)
        if (not match or not path.is_absolute() or path.name.lower() != "mumunxdevice.exe"
                or path.parents[2].name.lower() != "nx_device"):
            raise ValueError("无法识别 VM 名称或运行路径")
        dll = path.parent / "sdk" / "external_renderer_ipc.dll"
        if not dll.is_file():
            raise ValueError("当前 MuMu 安装目录没有原生截图 DLL")
        return NativeBinding(pid, port, str(path.parents[3]), int(match[2]), str(dll))
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError, IndexError) as exc:
        raise ClashAgentError(f"MuMu SDK 绑定失败：{exc}") from exc


def _native_worker(connection, binding: NativeBinding) -> None:
    # Vendor diagnostics must not corrupt the parent's CLI/GUI output. Errors
    # are returned over the private control pipe instead.
    null_output = open(os.devnull, "w")
    os.dup2(null_output.fileno(), 1)
    os.dup2(null_output.fileno(), 2)
    handle = 0
    sdk = None
    try:
        sdk = ctypes.CDLL(binding.dll)
        sdk.nemu_connect.argtypes = [ctypes.c_wchar_p, ctypes.c_int]
        sdk.nemu_connect.restype = ctypes.c_int
        sdk.nemu_disconnect.argtypes = [ctypes.c_int]
        sdk.nemu_get_display_id.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        sdk.nemu_get_display_id.restype = ctypes.c_int
        sdk.nemu_capture_display.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.c_void_p]
        sdk.nemu_capture_display.restype = ctypes.c_int
        handle = sdk.nemu_connect(binding.root, binding.index)
        if not handle:
            raise RuntimeError("MuMu SDK 无法连接已选实例")
        while True:
            message = connection.recv()
            if message[0] == "close":
                break
            _, memory_name = message
            # Capture the foreground renderer, like ADB. Never cache a fallback
            # display ID across game launch or tab changes.
            display = sdk.nemu_get_display_id(handle, b"default", 0)
            if display < 0:
                raise RuntimeError("MuMu SDK 当前没有可用的前台 display")
            width, height = ctypes.c_int(), ctypes.c_int()
            if sdk.nemu_capture_display(handle, display, 0, ctypes.byref(width), ctypes.byref(height), None) != 0:
                raise RuntimeError("MuMu SDK 无法查询画面尺寸")
            count = width.value * height.value * 4
            if width.value < 1 or height.value < 1 or not 0 < count <= _CAPACITY:
                raise RuntimeError("MuMu SDK 画面尺寸超出缓冲区限制")
            buffer = (ctypes.c_ubyte * count)()
            if sdk.nemu_capture_display(handle, display, count, ctypes.byref(width), ctypes.byref(height), buffer) != 0:
                raise RuntimeError("MuMu SDK 采集失败（可能发生方向或 display 变化）")
            if width.value * height.value * 4 != count:
                raise RuntimeError("MuMu SDK 采集期间画面尺寸变化")
            rgba = np.ctypeslib.as_array(buffer).reshape(height.value, width.value, 4)
            image = cv2.flip(cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR), 0)
            memory = shared_memory.SharedMemory(name=memory_name)
            try:
                target = np.ndarray(image.shape, dtype=np.uint8, buffer=memory.buf)
                target[:] = image
                del target
            finally:
                memory.close()
            connection.send(("ok", width.value, height.value))
    except (Exception, EOFError) as exc:
        try:
            connection.send(("error", str(exc)))
        except (OSError, EOFError):
            pass
    finally:
        if sdk is not None and handle:
            sdk.nemu_disconnect(handle)
        connection.close()
        null_output.close()


class MuMuNativeCapture:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.binding: NativeBinding | None = None
        self.process = None
        self.connection = None
        self.memory = None

    def capture(self) -> ScreenFrame:
        try:
            if self.binding is None:
                self.binding = resolve_binding(self.settings)
            owners = WindowsRuntime().listeners()
            if not any(item.pid == self.binding.pid and item.port == self.binding.port for item in owners):
                raise ClashAgentError("MuMu 实例已重启或地址变化，需重新绑定 SDK。")
            if self.process is None:
                context = multiprocessing.get_context("spawn")
                self.connection, child = context.Pipe()
                self.memory = shared_memory.SharedMemory(create=True, size=_CAPACITY)
                self.process = context.Process(target=_native_worker, args=(child, self.binding), daemon=True)
                self.process.start()
                child.close()
            self.connection.send(("capture", self.memory.name))
            if not self.connection.poll(4):
                raise ClashAgentError("MuMu SDK 截图超时，已结束独立截图进程。")
            response = self.connection.recv()
            if response[0] != "ok":
                raise ClashAgentError(f"MuMu SDK：{response[1]}")
            _, width, height = response
            if width < 1 or height < 1 or width * height * 3 > _CAPACITY:
                raise ClashAgentError("MuMu SDK 返回无效尺寸。")
            image = np.ndarray((height, width, 3), dtype=np.uint8, buffer=self.memory.buf)
            return ScreenFrame.from_bgr(image, self.settings.endpoint, backend="mumu_native")
        except Exception as exc:
            self.close()
            raise ClashAgentError(str(exc)) from exc

    def close(self) -> None:
        if self.process is not None:
            if self.process.is_alive():
                try:
                    self.connection.send(("close",))
                except (OSError, EOFError):
                    pass
                self.process.join(0.2)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(1)
            self.process.close()
            self.process = None
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.memory is not None:
            self.memory.close()
            self.memory.unlink()
            self.memory = None
