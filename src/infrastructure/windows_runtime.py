"""Read only running processes and their IPv4 listeners, without scanning installs."""

import ctypes
import os
import socket
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from core.errors import ClashAgentError

MUMU_DEVICE_NAMES = frozenset({
    "mumunxdevice.exe", "mumuvmmheadless.exe", "mumuheadless.exe", "nemuheadless.exe",
})


@dataclass(frozen=True)
class RuntimeProcess:
    pid: int
    name: str


@dataclass(frozen=True)
class Listener:
    pid: int
    address: str
    port: int


class WindowsRuntime:
    def processes(self) -> list[RuntimeProcess]:
        if os.name != "nt":
            raise ClashAgentError("自动发现 MuMu 当前仅支持 Windows，请使用手动地址。")
        class Entry(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                        ("pid", wintypes.DWORD), ("heap", ctypes.c_size_t),
                        ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                        ("parent", wintypes.DWORD), ("priority", wintypes.LONG),
                        ("flags", wintypes.DWORD), ("name", wintypes.WCHAR * 260)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
        kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateToolhelp32Snapshot(2, 0)
        if handle == ctypes.c_void_p(-1).value:
            raise ClashAgentError("无法读取运行中进程，请检查权限或切换为手动地址。")
        try:
            entry = Entry()
            entry.size = ctypes.sizeof(entry)
            result = []
            more = kernel.Process32FirstW(handle, ctypes.byref(entry))
            while more:
                if entry.name.lower() in MUMU_DEVICE_NAMES:
                    result.append(RuntimeProcess(entry.pid, entry.name))
                more = kernel.Process32NextW(handle, ctypes.byref(entry))
            return result
        finally:
            kernel.CloseHandle(handle)

    def executable_path(self, pid: int) -> Path | None:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
        ]
        kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            path = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(path))
            if kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)) and size.value:
                return Path(path.value)
            return None
        finally:
            kernel.CloseHandle(handle)

    def listeners(self) -> list[Listener]:
        if os.name != "nt":
            raise ClashAgentError("监听端口查询仅支持 Windows。")
        class Row(ctypes.Structure):
            _fields_ = [(name, wintypes.DWORD) for name in ("state", "local_address", "local_port", "remote_address", "remote_port", "pid")]
        iphlp = ctypes.WinDLL("iphlpapi", use_last_error=True)
        api = iphlp.GetExtendedTcpTable
        api.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
                        wintypes.ULONG, ctypes.c_int, wintypes.ULONG]
        api.restype = wintypes.DWORD
        size = wintypes.DWORD()
        code = api(None, ctypes.byref(size), False, socket.AF_INET, 3, 0)
        if code not in (0, 122):
            raise ClashAgentError(f"无法读取 TCP 监听表（错误码 {code}）。")
        for _ in range(3):
            buffer = ctypes.create_string_buffer(size.value)
            code = api(buffer, ctypes.byref(size), False, socket.AF_INET, 3, 0)
            if code != 122:
                break
        if code:
            raise ClashAgentError(f"TCP 监听表查询失败（错误码 {code}）。")
        count = wintypes.DWORD.from_buffer(buffer).value
        offset = ctypes.sizeof(wintypes.DWORD)
        result = []
        for index in range(count):
            row = Row.from_buffer(buffer, offset + index * ctypes.sizeof(Row))
            address = socket.inet_ntoa(int(row.local_address).to_bytes(4, "little"))
            if address == "0.0.0.0" or address.startswith("127."):
                result.append(Listener(row.pid, address, socket.ntohs(row.local_port & 65535)))
        return result
