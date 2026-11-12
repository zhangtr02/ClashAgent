"""Normalize zoom and center from measured camera pan limits, not red walls."""
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic, perf_counter

import cv2
import numpy as np

from core.errors import ClashAgentError


class CameraInterrupted(Exception):
    pass


def path(start, end, steps=10):
    return tuple((start[0]+(end[0]-start[0])*i/steps,
                  start[1]+(end[1]-start[1])*i/steps) for i in range(steps+1))


def motion(before, after, *, builder=False):
    """Robust screen translation/scale on map pixels, excluding the fixed HUD."""
    cv2.ocl.setUseOpenCL(False)
    mask = np.zeros((720, 1280), np.uint8)
    if builder:
        # Panning a Builder Base can push most buildings out of the central
        # band. Include dim rocks and trees around it, excluding the HUD.
        mask[90:563, 30:1250] = 255
        mask[449:, :616] = 0  # Boost buttons and the battle exit.
        mask[482:, 1042:] = 0  # Destruction and stars.
    else:
        mask[170:435, 180:1100] = 255
    images = [cv2.resize(f.gray, (1280, 720), interpolation=cv2.INTER_AREA) for f in (before, after)]
    orb = cv2.ORB_create(nfeatures=2600 if builder else 1600, fastThreshold=6 if builder else 12)
    ka, da = orb.detectAndCompute(images[0], mask)
    kb, db = orb.detectAndCompute(images[1], mask)
    if da is None or db is None:
        raise ClashAgentError("地图特征不足，无法确认视角。")
    pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(da, db, k=2)
    matches = [a for pair in pairs if len(pair) == 2 for a, b in [pair] if a.distance < .72*b.distance]
    if len(matches) < 16:
        raise ClashAgentError("无法确认地图视角，请检查过渡画面或遮挡。")
    pa = np.float32([ka[m.queryIdx].pt for m in matches])
    pb = np.float32([kb[m.trainIdx].pt for m in matches])
    matrix, inliers = cv2.estimateAffinePartial2D(pa, pb, method=cv2.RANSAC, ransacReprojThreshold=2.5)
    if matrix is None or int(inliers.sum()) < 12 or float(inliers.mean()) < .45:
        raise ClashAgentError("地图视角匹配不稳定，已阻止继续输入。")
    matched = pa[inliers.ravel() != 0]
    if np.ptp(matched[:, 0]) < 180 or np.ptp(matched[:, 1]) < 80:
        raise ClashAgentError("地图匹配点覆盖范围不足，无法确认视角。")
    a, b, dx = matrix[0]
    _, _, dy = matrix[1]
    return {"dx": float(dx), "dy": float(dy), "scale": float(np.hypot(a, b)),
            "rotation": float(np.degrees(np.arctan2(b, a))), "inliers": int(inliers.sum())}


def same_view(measured, tolerance=3):
    return (abs(measured["scale"]-1) < .007 and abs(measured["rotation"]) < .35
            and abs(measured["dx"]) <= tolerance and abs(measured["dy"]) <= tolerance)


@dataclass
class CameraReference:
    frame: object
    measure: Callable = motion

    def verify(self, frame):
        measured = self.measure(self.frame, frame)
        if not same_view(measured, 5):
            raise ClashAgentError("地图视角已偏移，已阻止使用旧落点；停止并重新开始以校准视角。")
        return measured


class CameraNormalizer:
    MAX_SECONDS = 18.0
    MAX_PAN_Y = 120
    LIMIT_ATTEMPTS = 6
    measure = staticmethod(motion)

    def __init__(self, device, control, checkpoint, notify, frame_check=None):
        self.device, self.control = device, control
        self.checkpoint, self.notify = checkpoint, notify
        self.frame_check = frame_check
        self.frame = None
        self.version = None
        self.deadline = None
        self.stats = {}

    def check_budget(self):
        # Game time keeps running during pause; do not renew this deadline.
        if self.deadline is not None and monotonic() >= self.deadline:
            raise ClashAgentError("地图校准超过 18 秒预算，已暂停；未开始下兵，请停止任务后检查截图速度。")

    def capture(self):
        self.check_budget()
        if self.checkpoint() != self.version:
            raise CameraInterrupted()
        start = perf_counter()
        self.frame = self.device.capture()
        self.stats["captures"] += 1
        self.stats["capture_ms"] += (perf_counter()-start)*1000
        self.check_budget()
        if self.checkpoint() != self.version:
            raise CameraInterrupted()
        if self.frame_check is not None and not self.frame_check(self.frame):
            self.notify("场景已变化，结束视角校准", {"interrupted": True}, self.frame)
            raise CameraInterrupted()
        return self.frame

    def stabilize(self):
        # Ordinary moves need one capture. Only a proposed limit or final
        # placement needs a second frame proving the map has stopped moving.
        for _ in range(2):
            previous = self.frame
            self.control.wait(.04)
            self.capture()
            if same_view(self.measure(previous, self.frame), 1.5):
                return
        raise ClashAgentError("触控结束后地图仍未稳定，未开始下兵。")

    def gesture(self, paths, label):
        self.check_budget()
        if self.checkpoint() != self.version:
            raise CameraInterrupted()
        # The current frame was validated by run()/capture().
        start = perf_counter()
        if not self.device.gesture(paths, self.frame, self.control, self.version):
            raise CameraInterrupted()
        self.stats["gestures"] += 1
        self.stats["gesture_ms"] += (perf_counter()-start)*1000
        # TouchChannel already holds fingers still before releasing them.
        self.control.wait(.06)
        self.capture()
        self.notify(label, {"paths": [[list(p[0]), list(p[-1])] for p in paths]}, self.frame)
        return self.frame

    def limit(self, start, end, label):
        total = np.zeros(2, dtype=float)
        dx, dy = end[0]-start[0], end[1]-start[1]
        factor = min(1, 400/max(abs(dx), 1), self.MAX_PAN_Y/max(abs(dy), 1))
        end = start[0]+dx*factor, start[1]+dy*factor
        for _ in range(self.LIMIT_ATTEMPTS):
            previous = self.frame
            self.gesture((path(start, end, 10),), label)
            measured = self.measure(previous, self.frame)
            if abs(measured["scale"]-1) > .007:
                raise ClashAgentError("拖动时地图缩放发生变化，无法固定视角。")
            total += measured["dx"], measured["dy"]
            if same_view(measured):
                before_settle = self.frame
                self.stabilize()
                drift = self.measure(before_settle, self.frame)
                total += drift["dx"], drift["dy"]
                return self.frame, total
        raise ClashAgentError("地图未到达稳定拖动边界，无法固定位置。")

    def run(self, frame):
        if abs(frame.width/frame.height-1280/720) > .01:
            raise ClashAgentError("地图校准要求 16:9 横屏。")
        self.frame, self.version = frame, self.checkpoint()
        start = monotonic()
        self.deadline = start+self.MAX_SECONDS
        self.stats = {"captures": 0, "capture_ms": 0.0, "gestures": 0, "gesture_ms": 0.0}
        if self.frame_check is not None and not self.frame_check(self.frame):
            raise CameraInterrupted()
        for attempt in range(4):
            previous = self.frame
            self.gesture((path((270, 340), (565, 340)), path((1010, 340), (715, 340))), "双指缩小地图")
            try:
                measured = self.measure(previous, self.frame)
            except ClashAgentError:
                continue
            if same_view(measured) and attempt >= 1:
                self.stabilize()
                break
        else:
            raise ClashAgentError("地图缩放未达到稳定极限，未开始下兵。")
        self.limit((860, 410), (220, 180), "测量地图左上拖动边界")
        _, total = self.limit((420, 250), (1040, 480), "测量地图右下拖动边界")
        limits = {"dx": float(total[0]), "dy": float(total[1]), "scale": 1.0}
        if abs(limits["scale"]-1) > .007 or abs(limits["dx"]) > 900 or abs(limits["dy"]) > 650:
            raise ClashAgentError("地图边界测量异常，未开始下兵。")
        dx, dy = -limits["dx"]/2, -limits["dy"]/2
        placed = {"dx": 0.0, "dy": 0.0, "scale": 1.0}
        drag_slop = 0.0
        for _ in range(5):
            remaining_x, remaining_y = dx-placed["dx"], dy-placed["dy"]
            if max(abs(remaining_x), abs(remaining_y)) <= 6:
                break
            before = self.frame
            move_x = float(np.clip(remaining_x, -320, 320))
            move_y = float(np.clip(remaining_y, -self.MAX_PAN_Y, self.MAX_PAN_Y))
            length = float(np.hypot(move_x, move_y))
            if length:
                move_x += move_x/length*drag_slop
                move_y += move_y/length*drag_slop
            self.gesture((path((640, 330), (640+move_x, 330+move_y), 10),), "固定地图到拖动范围中心")
            delta = self.measure(before, self.frame)
            if abs(delta["scale"]-1) > .007:
                raise ClashAgentError("居中时缩放发生变化，未开始下兵。")
            placed["dx"] += delta["dx"]
            placed["dy"] += delta["dy"]
            distance = float(np.hypot(delta["dx"], delta["dy"]))
            if distance < 1:
                drag_slop = max(drag_slop, 16.0)
            else:
                drag_slop = float(np.clip(np.hypot(move_x, move_y)-distance, 0, 24))
        previous = self.frame
        self.stabilize()
        drift = self.measure(previous, self.frame)
        placed["dx"] += drift["dx"]
        placed["dy"] += drift["dy"]
        self.notify("核验地图居中位置", {"pan_limits": limits, "centering": placed,
                    "expected": [dx, dy], "drag_slop": drag_slop}, self.frame)
        if (abs(placed["dx"]-dx) > 6 or abs(placed["dy"]-dy) > 6 or abs(placed["scale"]-1) > .007):
            raise ClashAgentError("地图未能固定到中心，未开始下兵。")
        self.check_budget()
        self.notify("视角已固定，开始验证候选点", {"pan_limits": limits, "centering": placed,
                    "elapsed_ms": (monotonic()-start)*1000, **self.stats}, self.frame)
        return CameraReference(self.frame, self.measure)


class BuilderCameraNormalizer(CameraNormalizer):
    MAX_PAN_Y = 95
    LIMIT_ATTEMPTS = 8

    @staticmethod
    def measure(before, after):
        return motion(before, after, builder=True)
