"""ADB smart-socket client. The CLI is only used to bootstrap the server."""

import os
import shlex
import socket
import struct
import subprocess
import threading
import time
from contextlib import contextmanager

from core.errors import ClashAgentError

_START_LOCK = threading.Lock()
_MAX_DATA = 64 * 1024 * 1024


def read_exact(stream: socket.socket, size: int, deadline: float) -> bytes:
    if not 0 <= size <= _MAX_DATA:
        raise ClashAgentError("ADB 响应长度异常。")
    data = bytearray()
    while len(data) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("ADB 响应超时")
        stream.settimeout(remaining)
        chunk = stream.recv(size - len(data))
        if not chunk:
            raise ClashAgentError("ADB 连接意外中断。")
        data.extend(chunk)
    return bytes(data)


def read_string(stream: socket.socket, deadline: float) -> str:
    try:
        size = int(read_exact(stream, 4, deadline), 16)
    except ValueError as exc:
        raise ClashAgentError("ADB 服务返回了无效长度。") from exc
    return read_exact(stream, size, deadline).decode("utf-8", errors="replace")


def request(stream: socket.socket, message: str, deadline: float) -> None:
    payload = message.encode("utf-8")
    if len(payload) > 65535:
        raise ClashAgentError("ADB 请求过长。")
    stream.sendall(f"{len(payload):04x}".encode("ascii") + payload)
    status = read_exact(stream, 4, deadline)
    if status == b"FAIL":
        raise ClashAgentError(f"ADB 服务拒绝请求：{read_string(stream, deadline)}")
    if status != b"OKAY":
        raise ClashAgentError("本地服务未返回有效 ADB 状态。")


class AdbServer:
    def __init__(self, executable: str | None, address: tuple[str, int] = ("127.0.0.1", 5037)):
        self.executable = executable
        self.address = address

    def _socket(self, timeout: float) -> socket.socket:
        try:
            return socket.create_connection(self.address, timeout=min(timeout, 1))
        except OSError:
            # Do not kill/restart an existing server or disrupt other clients.
            with _START_LOCK:
                try:
                    return socket.create_connection(self.address, timeout=min(timeout, 1))
                except OSError:
                    pass
                if not self.executable:
                    raise ClashAgentError("未找到可用的 ADB。请先打开 MuMu，或在「设备连接」中选择已有的 ADB 程序。")
                try:
                    result = subprocess.run(
                        [self.executable, "-P", str(self.address[1]), "start-server"],
                        capture_output=True, timeout=8,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise ClashAgentError(f"无法启动本地 ADB 服务：{exc}") from exc
                if result.returncode:
                    raise ClashAgentError("ADB 服务启动失败：" + result.stderr.decode("utf-8", errors="replace"))
                deadline = time.monotonic() + 2
                while True:
                    try:
                        return socket.create_connection(self.address, timeout=0.3)
                    except OSError as exc:
                        if time.monotonic() >= deadline:
                            raise ClashAgentError("本地 ADB 服务不可用；客户端虽已运行，但服务端口尚未就绪。") from exc
                        time.sleep(0.1)

    @contextmanager
    def _session(self, timeout: float):
        try:
            with self._socket(timeout) as stream:
                stream.settimeout(timeout)
                yield stream, time.monotonic() + timeout
        except TimeoutError as exc:
            raise ClashAgentError("ADB 服务操作超时，请检查设备连接。") from exc
        except OSError as exc:
            raise ClashAgentError(f"ADB 服务连接失败：{exc}") from exc

    def host(self, command: str, timeout: float = 5) -> str:
        with self._session(timeout) as (stream, deadline):
            request(stream, command, deadline)
            return read_string(stream, deadline)

    def shell(self, serial: str, args: tuple[str, ...], timeout: float) -> str:
        command = " ".join(shlex.quote(arg) for arg in args)
        with self._session(timeout) as (stream, deadline):
            request(stream, "host:transport:" + serial, deadline)
            request(stream, "shell,v2,raw:" + command, deadline)
            stdout, stderr = bytearray(), bytearray()
            while True:
                channel, size = struct.unpack("<BI", read_exact(stream, 5, deadline))
                packet = read_exact(stream, size, deadline)
                if channel == 1:
                    stdout.extend(packet)
                elif channel == 2:
                    stderr.extend(packet)
                elif channel == 3:
                    if not packet:
                        raise ClashAgentError("ADB shell 缺少退出状态。")
                    if packet[0] != 0:
                        detail = (stderr or stdout).decode("utf-8", errors="replace").strip()
                        raise ClashAgentError(detail or f"Android 命令返回错误码 {packet[0]}")
                    return stdout.decode("utf-8", errors="replace")
                if len(stdout) + len(stderr) > _MAX_DATA:
                    raise ClashAgentError("ADB shell 输出过大。")

    def screenshot(self, serial: str) -> bytes:
        return self.exec(serial, "screencap -p")

    def push(self, serial: str, remote: str, data: bytes) -> None:
        """Upload the bundled touch component using the standard sync service."""
        with self._session(10) as (stream, deadline):
            request(stream, "host:transport:" + serial, deadline)
            request(stream, "sync:", deadline)
            path = (remote+",33188").encode("utf-8")
            stream.sendall(b"SEND"+struct.pack("<I", len(path))+path)
            for start in range(0, len(data), 65536):
                part = data[start:start+65536]
                stream.sendall(b"DATA"+struct.pack("<I", len(part))+part)
            stream.sendall(b"DONE"+struct.pack("<I", int(time.time())))
            status, length = struct.unpack("<4sI", read_exact(stream, 8, deadline))
            if status != b"OKAY":
                detail = read_exact(stream, min(length, 4096), deadline).decode("utf-8", errors="replace")
                raise ClashAgentError("触控组件上传失败："+detail)

    def exec(self, serial: str, command: str, timeout: float = 15) -> bytes:
        with self._session(timeout) as (stream, deadline):
            request(stream, "host:transport:" + serial, deadline)
            request(stream, "exec:" + command, deadline)
            data = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                stream.settimeout(remaining)
                chunk = stream.recv(1024 * 1024)
                if not chunk:
                    return bytes(data)
                data.extend(chunk)
                if len(data) > _MAX_DATA:
                    raise ClashAgentError("ADB 截图输出过大。")
