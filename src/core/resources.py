"""Village HUD quantities and the single farming stop condition."""
from dataclasses import dataclass

import cv2
import numpy as np

from core.capture import ScreenFrame
from core.errors import ClashAgentError
from core.vision.models import Region

RESOURCE_LABELS = {"gold": "金币", "elixir": "圣水", "dark_elixir": "黑油"}


@dataclass(frozen=True)
class ResourceGoal:
    resource: str
    capacity: int
    percent: int = 90

    def validate(self) -> None:
        if self.resource not in RESOURCE_LABELS:
            raise ClashAgentError("不支持的资源类型。")
        if type(self.capacity) is not int or not 1 <= self.capacity <= 2_000_000_000:
            raise ClashAgentError("资源最大容量未读取或不合法。")
        if type(self.percent) is not int or not 10 <= self.percent <= 100:
            raise ClashAgentError("资源停止比例应为 10%–100%。")


@dataclass(frozen=True)
class ResourceReading:
    resource: str
    value: int
    capacity: int
    target_percent: int
    confidence: float

    @property
    def percent(self) -> float:
        return self.value * 100 / self.capacity

    @property
    def reached(self) -> bool:
        # Integer arithmetic avoids rounding a value just below the threshold up.
        return self.value * 100 >= self.capacity * self.target_percent


@dataclass(frozen=True)
class FarmOptions:
    goals: tuple[ResourceGoal, ...] = ()

    def validate(self) -> None:
        if len(self.goals) > 3 or len({g.resource for g in self.goals}) != len(self.goals):
            raise ClashAgentError("资源停止条件不能重复。")
        for goal in self.goals:
            goal.validate()


class ResourceTargetReached(Exception):
    """An expected successful exit, raised before the next search input."""


def white_mask(image: np.ndarray) -> np.ndarray:
    low = image.min(axis=2)
    high = image.max(axis=2)
    return ((low >= 210) & ((high.astype(np.int16)-low) <= 55)).astype(np.uint8)


def normalize_digit(mask: np.ndarray) -> np.ndarray:
    ys, xs = np.where(mask)
    if not len(xs):
        raise ClashAgentError("资源数字模板为空。")
    crop = mask[ys.min():ys.max()+1, xs.min():xs.max()+1]
    scale = min(18/crop.shape[1], 26/crop.shape[0])
    w, h = max(1, round(crop.shape[1]*scale)), max(1, round(crop.shape[0]*scale))
    normalized = np.zeros((28, 20), np.float32)
    normalized[(28-h)//2:(28-h)//2+h, (20-w)//2:(20-w)//2+w] = cv2.resize(
        crop.astype(np.float32), (w, h), interpolation=cv2.INTER_AREA)
    return normalized


class ResourceReader:
    """Small, local digit atlas; no OCR runtime/model download or game inputs."""
    def __init__(self, digits: dict[str, tuple[np.ndarray, ...]], regions: dict[str, Region], capacity_label=None):
        self.digits, self.regions = digits, regions
        self.capacity_label = capacity_label
        self._frame = self._pixels = None

    def pixels(self, frame):
        if abs(frame.width/frame.height-1280/720) > .01:
            raise ClashAgentError("资源识别要求 16:9 横屏。")
        if self._frame is not frame:
            self._pixels = cv2.resize(frame.image, (1280, 720), interpolation=cv2.INTER_AREA)
            self._frame = frame
        return self._pixels

    def capacity_popup(self, frame):
        """Return the owning resource and exact 'maximum storage' label box."""
        if self.capacity_label is None:
            raise ClashAgentError("缺少最大储量识别素材。")
        pixels = self.pixels(frame)
        matches = []
        for resource, hud in self.regions.items():
            x, y = 980, hud.y+35
            mask = white_mask(pixels[y:y+52, x:1260])*255
            best = None
            for template in self.capacity_label:
                _, score, _, point = cv2.minMaxLoc(cv2.matchTemplate(mask, template, cv2.TM_CCOEFF_NORMED))
                if best is None or score > best[0]:
                    best = score, point, template.shape
            score, point, (h, w) = best
            if score >= .88:
                matches.append((resource, Region(x+point[0], y+point[1], w, h)))
        return matches[0] if len(matches) == 1 else None

    def read_capacity(self, frame, resource):
        popup = self.capacity_popup(frame)
        if popup is None or popup[0] != resource:
            return None
        label = popup[1]
        # The numeral baseline is fixed relative to the matched label, while
        # tooltip width/position varies for gold, elixir and dark elixir.
        x, y = label.x+86, max(0, label.y-5)
        value = self.read_number(self.pixels(frame)[y:label.y+23, x:1250])
        if value is None or not 0 < value[0] <= 2_000_000_000:
            return None
        return value

    def resource_target(self, resource):
        r = self.regions[resource]
        return r.x+r.width//2, r.y+r.height//2

    def read_number(self, image: np.ndarray, *, skip_prefix=False) -> tuple[int, float] | None:
        return self.read_mask(white_mask(image), skip_prefix=skip_prefix)

    def read_mask(self, mask: np.ndarray, *, skip_prefix=False) -> tuple[int, float] | None:
        # Troop counters supply the same mask used to validate their x prefix.
        # Keep the component filters identical, so a small x cannot disappear
        # before skipping it and accidentally discard the first actual digit.
        _, _, stats, _ = cv2.connectedComponentsWithStats(mask)
        min_height, min_width, min_area = (8, 3, 12) if skip_prefix else (9, 2, 15)
        components = sorted((tuple(map(int, s)) for s in stats[1:]
                             if min_height <= s[3] <= 24 and min_width <= s[2] <= 24 and s[4] >= min_area), key=lambda s: s[0])
        if skip_prefix:
            if len(components) < 2:
                return None
            components = components[1:]
        if not 1 <= len(components) <= 10:
            return None
        heights = [component[3] for component in components]
        baseline = float(np.median([y+h for _, y, _, h, _ in components]))
        if max(heights)-min(heights) > 5 or any(abs(y+h-baseline) > 3 for _, y, _, h, _ in components):
            return None
        values, confidence = [], 1.0
        for x, y, w, h, _ in components:
            glyph = normalize_digit(mask[y:y+h, x:x+w])
            scores = []
            for digit, variants in self.digits.items():
                score = max(float(cv2.matchTemplate(glyph, variant, cv2.TM_CCOEFF_NORMED)[0, 0])
                            for variant in variants)
                scores.append((score, digit))
            scores.sort(reverse=True)
            score, digit = scores[0]
            if not np.isfinite(score) or score < .78 or score-scores[1][0] < .035:
                return None
            values.append(digit)
            confidence = min(confidence, score)
        if len(values) > 1 and values[0] == "0":
            return None
        return int("".join(values)), confidence

    def read(self, frame: ScreenFrame, goals: tuple[ResourceGoal, ...]) -> tuple[ResourceReading, ...] | None:
        pixels = self.pixels(frame)
        readings = []
        for goal in goals:
            if goal.capacity <= 0:
                raise ClashAgentError("最大容量尚未通过自动识别，已阻止判断资源比例。")
            r = self.regions[goal.resource]
            result = self.read_number(pixels[r.y:r.y+r.height, r.x:r.x+r.width])
            if result is None:
                return None
            value, score = result
            readings.append(ResourceReading(goal.resource, value, goal.capacity, goal.percent, score))
        return tuple(readings)


@dataclass
class ResourceGuard:
    """Require three identical fresh readings before allowing another search."""
    previous: tuple = ()
    count: int = 0

    def reset(self) -> None:
        self.previous, self.count = (), 0

    def accept(self, readings: tuple[ResourceReading, ...] | None) -> bool:
        if not readings or any(not np.isfinite(r.confidence) or r.confidence < .78 for r in readings):
            self.reset()
            return False
        key = tuple((r.resource, r.value, r.capacity, r.target_percent) for r in readings)
        self.count = self.count+1 if key == self.previous else 1
        self.previous = key
        return self.count >= 3
