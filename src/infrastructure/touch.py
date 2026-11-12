"""Run-owned MaaTouch channel for interruptible camera gestures and taps."""
import re
import select
import socket
import struct
import time

from core.errors import ClashAgentError
from infrastructure.adb_server import read_exact, request
from infrastructure.settings import PROJECT_ROOT


class TouchChannel:
    REMOTE = "/data/local/tmp/clashagent-maatouch.jar"

    def __init__(self, client):
        self.client = client
        self.stream = None
        self.pid = None
        self.size = None

    def _packet(self, deadline):
        channel, size = struct.unpack("<BI", read_exact(self.stream, 5, deadline))
        if size > 65536:
            raise ClashAgentError("触控组件响应过大。")
        data = read_exact(self.stream, size, deadline)
        if channel in (2, 3):
            raise ClashAgentError("触控组件异常："+data.decode("utf-8", errors="replace")[:500])
        return data

    def open(self, width, height):
        if self.stream is not None:
            if self.size != (width, height):
                raise ClashAgentError("触控画面尺寸已改变，请重新开始任务。")
            return
        jar = PROJECT_ROOT / "3rdparty/maatouch/maatouch.jar"
        if not jar.is_file():
            raise ClashAgentError("缺少 3rdparty/maatouch/maatouch.jar，无法统一地图视角。")
        self.client.server.push(self.client.serial, self.REMOTE, jar.read_bytes())
        stream = self.client.server._socket(5)
        self.stream = stream
        try:
            stream.settimeout(5)
            stream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            deadline = time.monotonic()+5
            request(stream, "host:transport:"+self.client.serial, deadline)
            command = ("echo CLASHAGENT_TOUCH_PID=$$; export CLASSPATH="+self.REMOTE
                       +"; exec app_process /data/local/tmp com.shxyke.MaaTouch.App")
            request(stream, "shell,v2,raw:"+command, deadline)
            greeting = b""
            while not (match := re.search(rb"\^ (\d+) (\d+) (\d+) (\d+)\s+\$", greeting)):
                greeting += self._packet(deadline)
                pid = re.search(rb"CLASHAGENT_TOUCH_PID=(\d+)", greeting)
                if pid:
                    self.pid = int(pid[1])
                if len(greeting) > 8192:
                    raise ClashAgentError("触控组件握手无效。")
            contacts, w, h, pressure = map(int, match.groups())
            if contacts < 2 or pressure != 255 or (w, h) != (width, height):
                raise ClashAgentError(f"触控坐标与截图不一致：{w}×{h} / {width}×{height}。")
            self.size = width, height
        except Exception:
            self.close()
            raise

    def send(self, commands):
        data = commands.encode("ascii")
        if self.stream is None:
            raise ClashAgentError("触控通道未初始化。")
        if select.select([self.stream], [], [], 0)[0]:
            self._packet(time.monotonic()+1)
        self.stream.sendall(struct.pack("<BI", 0, len(data))+data)

    def tap(self, x, y, width, height, control, version):
        """One short contact, never a queued series or a multi-finger pinch."""
        if type(x) is not int or type(y) is not int or not 0 <= x < width or not 0 <= y < height:
            raise ClashAgentError("点击坐标超出实际画面。")
        self.open(width, height)
        accepted = False
        try:
            accepted = control.dispatch(version, lambda: self.send(f"d 0 {x} {y} 255\nc\n"))
            if not accepted:
                return False
            control.wait(.025)
        finally:
            # Release an accepted contact even if pause/stop was requested
            # during the hold. Never leave a finger down between corners.
            if accepted:
                self.send("u 0\nc\n")
        return True

    def gesture(self, paths, width, height, control, version):
        """Each movement is gated; accepted contacts are always released."""
        self.open(width, height)
        scaled = [tuple((round(x*width/1280), round(y*height/720)) for x, y in path) for path in paths]
        if not scaled or len({len(path) for path in scaled}) != 1:
            raise ClashAgentError("触控路径无效。")
        for path in scaled:
            if any(not 0 <= x < width or not 0 <= y < height for x, y in path):
                raise ClashAgentError("触控路径超出画面。")
        accepted = False
        try:
            down = "".join(f"d {i} {path[0][0]} {path[0][1]} 255\n" for i, path in enumerate(scaled))+"c\n"
            accepted = control.dispatch(version, lambda: self.send(down))
            if not accepted:
                return False
            control.wait(.03)
            for step in range(1, len(scaled[0])):
                moves = "".join(f"m {i} {path[step][0]} {path[step][1]} 255\n" for i, path in enumerate(scaled))+"c\n"
                if not control.dispatch(version, lambda: self.send(moves)):
                    return False
                control.wait(.025)
            # End at zero velocity so Android/game fling does not move the
            # camera after the position measurement.
            control.wait(.18)
            return True
        finally:
            if accepted:
                self.send("".join(f"u {i}\n" for i in reversed(range(len(scaled))))+"c\n")

    def close(self):
        if self.stream is not None:
            try:
                self.send("r\nc\n")
            except Exception:
                pass
            self.stream.close()
            self.stream = None
        if self.pid is not None:
            pid, self.pid = self.pid, None
            # Only terminate the process launched by this channel, after checking
            # its command line; never kill another client's touch server.
            command = (f"case \"$(cat /proc/{pid}/cmdline 2>/dev/null)\" in "
                       f"*com.shxyke.MaaTouch.App*) kill {pid};; esac")
            self.client.shell("sh", "-c", command, timeout=3)
        self.size = None
