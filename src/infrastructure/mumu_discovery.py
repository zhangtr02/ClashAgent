import socket
import struct
import time
from concurrent.futures import ThreadPoolExecutor

from core.discovery import MuMuInstance
from infrastructure.windows_runtime import WindowsRuntime


def probe_adb(port: int, timeout: float = 0.4) -> bool:
    """Recognize CNXN/AUTH; an arbitrary open TCP port is not an ADB device."""
    command = int.from_bytes(b"CNXN", "little")
    payload = b"host::\0"
    packet = struct.pack("<6I", command, 0x01000000, 1024 * 1024,
                         len(payload), sum(payload), command ^ 0xFFFFFFFF) + payload
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as stream:
            stream.sendall(packet)
            header = bytearray()
            deadline = time.monotonic() + timeout
            while len(header) < 24:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                stream.settimeout(remaining)
                data = stream.recv(24 - len(header))
                if not data:
                    return False
                header.extend(data)
            code, _, _, length, _, magic = struct.unpack("<6I", header)
            return (code in (int.from_bytes(b"CNXN", "little"), int.from_bytes(b"AUTH", "little"))
                    and magic == code ^ 0xFFFFFFFF and length <= 1024 * 1024)
    except OSError:
        return False


class MuMuDiscovery:
    def __init__(self, runtime=None, probe=None):
        self.runtime = runtime or WindowsRuntime()
        self.probe = probe or probe_adb

    def discover(self, online_serials: tuple[str, ...] = ()) -> tuple[MuMuInstance, ...]:
        processes = self.runtime.processes()
        if not processes:
            return ()
        pids = {process.pid for process in processes}
        listeners = {(row.pid, row.port) for row in self.runtime.listeners() if row.pid in pids}
        # Only probe listeners owned by live MuMu VMs, never a fixed port range.
        ports = sorted({port for _, port in listeners})
        known = {int(serial.rsplit(":", 1)[1]) for serial in online_serials
                 if serial.startswith("127.0.0.1:") and serial.rsplit(":", 1)[1].isdigit()}
        with ThreadPoolExecutor(max_workers=8) as executor:
            verified = dict(zip(ports, executor.map(lambda port: port in known or self.probe(port), ports)))
        result = []
        for process in sorted(processes, key=lambda item: item.pid):
            endpoints = tuple(f"127.0.0.1:{port}" for pid, port in sorted(listeners, key=lambda item: -item[1])
                              if pid == process.pid and verified[port])
            result.append(MuMuInstance(f"pid:{process.pid}", process.pid, process.name, endpoints))
        return tuple(result)
