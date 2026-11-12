from dataclasses import dataclass, field, replace

import cv2
import numpy as np

from core.capture import ScreenFrame
from core.errors import ClashAgentError
from core.flow.models import Anchor
from core.vision.models import MatchResult, Region, TemplateProfile
from core.vision.matcher import match_template
from core.vision.template_cache import TemplateCache


@dataclass(frozen=True)
class UnitSpec:
    name: str
    label: str
    kind: str
    portrait: Anchor
    counter_offset: tuple[int, int, int, int] | None = None
    hero_card: Region | None = None


@dataclass(frozen=True)
class DeploymentPlan:
    units: tuple[UnitSpec, ...]
    prefix_png: bytes = field(repr=False)
    zero_png: bytes = field(repr=False)
    width: int = 1280
    height: int = 720
    blocked_png: bytes = field(default=b"", repr=False)


@dataclass(frozen=True)
class CardObservation:
    name: str
    status: str  # available, empty, deployed, unknown
    region: Region | None = None
    counter: bytes | None = field(default=None, repr=False)
    counter_pose: tuple[int, int] | None = None
    quantity: int | None = None
    selected: bool = False


def glyph_mask(image):
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    return ((hsv[:, :, 2] > 210) & (hsv[:, :, 1] < 75)).astype(np.uint8)


def normalize_glyph(mask, width=20, height=28):
    y, x = np.where(mask > 0)
    if not len(x):
        return np.zeros((height, width), np.uint8)
    crop = mask[y.min():y.max()+1, x.min():x.max()+1]
    return cv2.resize(crop, (width, height), interpolation=cv2.INTER_NEAREST)


def overlap(a, b):
    return float(np.sum((a > 0) & (b > 0)) / max(1, np.sum((a > 0) | (b > 0))))


class CardDetector:
    """Read visible portrait/counter changes, without estimating army size."""
    def __init__(self, plan: DeploymentPlan, number_reader=None):
        self.base_plan = self.plan = plan
        self.cache = TemplateCache()
        self.previous_slots = None
        self.number_reader = number_reader
        self.blocked_template = self.cache.prepare(plan.blocked_png).gray if plan.blocked_png else None
        self.prefix = normalize_glyph(glyph_mask(cv2.imdecode(np.frombuffer(plan.prefix_png, np.uint8), cv2.IMREAD_COLOR)))
        self.zero = normalize_glyph(glyph_mask(cv2.imdecode(np.frombuffer(plan.zero_png, np.uint8), cv2.IMREAD_COLOR)))

    def reset(self):
        self.plan = self.base_plan
        self.previous_slots = None

    @staticmethod
    def hero_slots(pixels):
        """Purple hero frames plus a square equipment badge; no hero-name list."""
        hsv = cv2.cvtColor(pixels, cv2.COLOR_BGR2HSV)
        bar = hsv[560:719, 75:1205]
        purple = ((bar[:, :, 0] >= 120) & (bar[:, :, 0] <= 150)
                  & (bar[:, :, 1] > 65) & (bar[:, :, 2] > 100)).astype(np.uint8)
        _, _, stats, _ = cv2.connectedComponentsWithStats(purple)
        slots = []
        for x, y, w, h, _ in stats[1:]:
            x, y, w, h = int(x+75), int(y+560), int(w), int(h)
            if not (45 <= w <= 115 and 85 <= h <= 150 and y+h >= 700):
                continue
            badge = hsv[y+1:y+40, x+1:x+40]
            white = ((badge[:, :, 1] < 85) & (badge[:, :, 2] > 150)).astype(np.uint8)
            white = cv2.morphologyEx(white, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
            _, _, pieces, _ = cv2.connectedComponentsWithStats(white)
            if not any(20 <= bw <= 37 and 20 <= bh <= 37 and abs(bw-bh) <= 8 and area >= 100
                       for _, _, bw, bh, area in pieces[1:]):
                continue
            slots.append(Region(x, y, w, h))
        return tuple(sorted(slots, key=lambda r: r.x))

    def prepare(self, frame: ScreenFrame) -> DeploymentPlan | None:
        pixels = cv2.resize(frame.image, (self.plan.width, self.plan.height), interpolation=cv2.INTER_AREA)
        scaled = ScreenFrame.from_bgr(pixels, frame.source)
        # Do not freeze an empty hero list while the entire bar is still loading.
        if not self._match(scaled, self.base_plan.units[0].portrait).matched:
            return None
        slots = self.hero_slots(pixels)
        key = tuple((round(r.x/4), round(r.y/4), round(r.width/4), round(r.height/4)) for r in slots)
        if key != self.previous_slots:
            self.previous_slots = key
            return None
        if len(slots) > 6:
            raise ClashAgentError("英雄卡槽数量异常，请检查当前兵栏。")
        heroes = []
        for index, r in enumerate(slots, 1):
            # Capture only the face, excluding equipment, level and health bar.
            portrait = Region(r.x+20, r.y+34, r.width-40, 34)
            ok, png = cv2.imencode('.png', pixels[portrait.y:portrait.y+portrait.height,
                                                      portrait.x:portrait.x+portrait.width])
            if not ok:
                raise ClashAgentError("英雄卡片读取失败。")
            anchor = Anchor(f"hero_{index}", png.tobytes(), Region(75, 560, 1130, 159), .88, 35)
            heroes.append(UnitSpec(f"hero_{index}", f"英雄{index}", "hero", anchor, hero_card=r))
        self.plan = replace(self.base_plan, units=self.base_plan.units+tuple(heroes))
        return self.plan

    @staticmethod
    def hero_deployed(pixels, card):
        # Require the health bar's inset geometry and dark frame. Map grass or
        # a red portrait above an unused slot is not deployment evidence.
        top = max(0, card.y-40)
        patch = pixels[top:card.y+5, card.x:card.x+card.width]
        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
        mask = (((hsv[:, :, 0] < 10) | (hsv[:, :, 0] > 173)
                 | ((hsv[:, :, 0] > 32) & (hsv[:, :, 0] < 90)))
                & (hsv[:, :, 1] > 100) & (hsv[:, :, 2] > 95)).astype(np.uint8)
        _, _, stats, _ = cv2.connectedComponentsWithStats(mask)
        for x, y, w, h, area in stats[1:]:
            if not (x >= 3 and x+w <= card.width-3 and w >= card.width*.65
                    and 3 <= h <= 14 and area >= w*2 and top+y+h <= card.y-6):
                continue
            border = gray[max(0, y-5):y, x:x+w]
            if border.size and int(np.sum(np.mean(border < 80, axis=1) >= .8)) >= 2:
                return True
        return False

    @staticmethod
    def hero_selected(pixels, card):
        white = (pixels.min(axis=2) > 190) & ((pixels.max(axis=2).astype(np.int16)-pixels.min(axis=2)) < 60)
        y1, y2 = card.y+10, card.y+card.height-10
        left = white[y1:y2, max(0, card.x-10):card.x+5]
        right = white[y1:y2, card.x+card.width-5:min(1280, card.x+card.width+10)]
        return bool(left.size and right.size and left.any(axis=1).mean() >= .75 and right.any(axis=1).mean() >= .75)

    def deployment_rejected(self, frame):
        if self.blocked_template is None:
            return False
        pixels = cv2.resize(frame.image, (1280, 720), interpolation=cv2.INTER_AREA)[160:245, 420:870]
        hsv = cv2.cvtColor(pixels, cv2.COLOR_BGR2HSV)
        red = (((hsv[:, :, 0] < 10) | (hsv[:, :, 0] > 173)) & (hsv[:, :, 1] > 150)
               & (hsv[:, :, 2] > 100) & (pixels[:, :, 2].astype(np.int16)-pixels[:, :, 1] > 50)).astype(np.uint8)*255
        score = cv2.minMaxLoc(cv2.matchTemplate(red, self.blocked_template, cv2.TM_CCOEFF_NORMED))[1]
        return score >= .9

    def _match(self, frame, anchor, scales=(1,)):
        template = self.cache.prepare(anchor.png).gray
        h, w = template.shape
        profile = TemplateProfile(anchor.name, anchor.name, self.plan.width, self.plan.height,
                                  Region(0, 0, w, h), anchor.roi, anchor.threshold)
        result = match_template(frame, profile, anchor.png, cache=self.cache)
        for scale in scales:
            if scale == 1:
                continue
            tw, th = round(w*scale), round(h*scale)
            resized = cv2.resize(template, (tw, th), interpolation=cv2.INTER_LINEAR)
            roi = anchor.roi
            search = frame.gray[roi.y:roi.y+roi.height, roi.x:roi.x+roi.width]
            if tw > search.shape[1] or th > search.shape[0]:
                continue
            _, score, _, point = cv2.minMaxLoc(cv2.matchTemplate(search, resized, cv2.TM_CCOEFF_NORMED))
            if score > result.score:
                result = MatchResult(anchor.name, frame.frame_id, Region(roi.x+point[0], roi.y+point[1], tw, th),
                                     score, anchor.threshold)
        return result

    def counter(self, pixels):
        mask = glyph_mask(pixels)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
        pieces = []
        for x, y, w, h, area in stats[1:]:
            if 8 <= h <= 24 and 3 <= w <= 24 and area >= 12:
                pieces.append((x, normalize_glyph(mask[y:y+h, x:x+w])))
        pieces.sort(key=lambda item: item[0])
        if not 2 <= len(pieces) <= 4 or overlap(pieces[0][1], self.prefix) < .65:
            return None, False
        empty = len(pieces) == 2 and overlap(pieces[-1][1], self.zero) > .86
        # Ignore card lift/selection geometry, retain the actual quantity glyphs.
        return b"".join(piece.tobytes() for _, piece in pieces[1:]), empty

    def observe(self, frame: ScreenFrame) -> tuple[CardObservation, ...]:
        if abs(frame.width / frame.height - self.plan.width / self.plan.height) > .01:
            raise ClashAgentError("投放资源要求 16:9 横屏。")
        pixels = cv2.resize(frame.image, (self.plan.width, self.plan.height), interpolation=cv2.INTER_AREA)
        scaled = ScreenFrame.from_bgr(pixels, frame.source)
        observations = []
        for unit in self.plan.units:
            if unit.hero_card and self.hero_deployed(pixels, unit.hero_card):
                observations.append(CardObservation(unit.name, "deployed"))
                continue
            # Selecting a card also enlarges its portrait; keep an explicit,
            # narrow scale range rather than lowering the recognition threshold.
            result = self._match(scaled, unit.portrait, (1, 1.04, 1.06, 1.08))
            if not result.matched:
                observations.append(CardObservation(unit.name, "unknown"))
                continue
            r = result.region
            patch = pixels[r.y:r.y+r.height, r.x:r.x+r.width]
            saturation = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)[:, :, 1]
            if float(saturation.mean()) < 25:
                observations.append(CardObservation(unit.name, "empty", r))
                continue
            template = self.cache.prepare(unit.portrait.png).gray
            sx, sy = r.width/template.shape[1], r.height/template.shape[0]
            if patch.shape[:2] != template.shape:
                template = cv2.resize(template, (r.width, r.height), interpolation=cv2.INTER_LINEAR)
            error = float(np.abs(scaled.gray[r.y:r.y+r.height, r.x:r.x+r.width].astype(float) - template).mean())
            if error > unit.portrait.max_pixel_error:
                observations.append(CardObservation(unit.name, "unknown", r))
                continue
            token, empty, quantity = None, False, None
            if unit.counter_offset:
                dx, dy, w, h = unit.counter_offset
                dx, dy, w, h = round(dx*sx), round(dy*sy), round(w*sx), round(h*sy)
                box = Region(r.x+dx, r.y+dy, w, h)
                box.validate(self.plan.width, self.plan.height)
                counter_pixels = pixels[box.y:box.y+h, box.x:box.x+w]
                token, empty = self.counter(counter_pixels)
                if token is not None and self.number_reader is not None:
                    number = self.number_reader.read_mask(glyph_mask(counter_pixels), skip_prefix=True)
                    quantity = number[0] if number is not None and number[0] <= 300 else None
                    empty = empty or quantity == 0
            status = "empty" if empty else ("unknown" if unit.kind == "troop" and token is None else "available")
            selected = self.hero_selected(pixels, unit.hero_card) if unit.hero_card else False
            observations.append(CardObservation(unit.name, status, r, token, (r.width, r.height), quantity, selected))
        return tuple(observations)

@dataclass(frozen=True)
class DeploymentAction:
    kind: str
    unit: str
    x: int = 0
    y: int = 0
    count: int = 0
    counter: bytes | None = field(default=None, repr=False)
    counter_pose: tuple[int, int] | None = None
    corner_offset: int = 0
    probe: bool = False
    quantity_before: int | None = None


@dataclass
class PendingBurst:
    action: DeploymentAction
    started: float
    sent: int = 0
    evidence: bytes | str | None = field(default=None, repr=False)
    evidence_count: int = 0


class DeploymentController:
    """One fixed army: opening wave, heroes, then the remaining troops."""
    BURST_SIZE = 16
    SELECTION_DELAY = .15
    TAP_INTERVAL = .015
    MAX_TROOP_TAPS = 80

    def __init__(self, plan: DeploymentPlan):
        self.plan = plan
        self.units = plan.units
        self.done: set[str] = set()
        self.sent = {unit.name: 0 for unit in self.units}
        self.confirmed = {unit.name: 0 for unit in self.units}
        self.stalls = {unit.name: 0 for unit in self.units}
        self.selected = ""
        self.selected_at = 0.0
        self.pending: PendingBurst | None = None
        self.opening_verified = False
        self.retry_generation = 0
        self.skip_evidence = {}

    @property
    def finished(self) -> bool:
        return len(self.done) == len(self.units)

    def resumed(self, now=0) -> None:
        self.selected = ""
        self.skip_evidence.clear()
        if self.pending:
            if self.pending.sent == 0:
                self.pending = None
            else:
                self.pending.started = now
                self.pending.evidence = None
                self.pending.evidence_count = 0

    @staticmethod
    def changed(before, after) -> bool:
        if before is None or after is None:
            return False
        if len(before) != len(after):
            return True
        return float(np.mean(np.frombuffer(before, np.uint8) != np.frombuffer(after, np.uint8))) > .035

    def select_accepted(self, action: DeploymentAction, now: float) -> None:
        self.selected, self.selected_at = action.unit, now

    def begin_burst(self, action: DeploymentAction, now: float) -> None:
        if self.pending is not None:
            raise ClashAgentError("上一批投放尚未确认。")
        self.pending = PendingBurst(action, now)

    def tap_accepted(self) -> None:
        if self.pending is None:
            raise ClashAgentError("缺少投放批次。")
        self.pending.sent += 1
        self.sent[self.pending.action.unit] += 1

    def end_burst(self, now: float) -> None:
        if self.pending is not None:
            self.pending.started = now

    def _select(self, unit, card):
        if card.region is None:
            raise ClashAgentError(f"无法确认{unit.label}卡片位置。")
        r = card.region
        return DeploymentAction("select", unit.name, r.x+r.width//2, r.y+r.height//2)

    def _verify_pending(self, observations, now, deployment_rejected=False):
        pending = self.pending
        unit = next(u for u in self.units if u.name == pending.action.unit)
        card = observations.get(unit.name)
        if pending.sent == 0:
            self.pending = None
            return None
        if card is None or card.status == "unknown":
            if now-pending.started < 1.5:
                return None
            raise ClashAgentError(f"暂时无法确认{unit.label}，请检查兵栏后继续。")
        if (unit.kind == "troop" and not pending.action.probe and card.status == "available" and pending.action.counter_pose
                and (card.counter_pose is None or
                     max(abs(a-b) for a, b in zip(card.counter_pose, pending.action.counter_pose)) > 2)):
            if self.selected != unit.name:
                return self._select(unit, card)
            if now-max(self.selected_at, pending.started) < 1.5:
                return None
            raise ClashAgentError(f"{unit.label}卡片仍在变化，等待确认上次投放后继续。")
        progressed = (card.status in ("empty", "deployed") or
                      (unit.kind == "troop" and self.changed(pending.action.counter, card.counter)))
        evidence = card.counter if card.status == "available" else card.status
        if pending.action.probe:
            remaining = 0 if card.status == "empty" else card.quantity
            if remaining is None:
                if now-pending.started < 1.5:
                    return None
                raise ClashAgentError("试投后无法可靠读取兵数，不能将该点标记为合法。")
            difference = pending.action.quantity_before-remaining
            if difference < 0 or difference > pending.sent:
                raise ClashAgentError("试投兵数变化与单次输入不一致，请检查其他操作来源。")
            progressed = difference == pending.sent
            evidence = remaining
        if progressed:
            pending.evidence_count = pending.evidence_count+1 if evidence == pending.evidence else 1
            pending.evidence = evidence
            if pending.evidence_count >= 2:
                self.confirmed[unit.name] += 1
                self.stalls[unit.name] = 0
                if unit.kind == "troop":
                    self.opening_verified = True
                if unit.kind == "hero" or card.status == "empty":
                    self.done.add(unit.name)
                self.pending = None
            return None
        pending.evidence = None
        pending.evidence_count = 0
        # A faster poll must not shorten the game's hero spawn animation.
        timeout = 1.5 if unit.kind == "hero" and not deployment_rejected else .8
        if now-pending.started < timeout:
            return None
        if pending.action.probe:
            self.pending = None
            self.retry_generation += 1
            return None
        if unit.kind == "hero":
            if deployment_rejected and card.status == "available" and card.selected:
                if self.sent[unit.name] >= 3:
                    raise ClashAgentError(f"游戏连续拒绝{unit.label}的落点，已暂停；请检查地图边缘。")
                self.pending = None
                self.stalls[unit.name] += 1
                self.retry_generation += 1
                self.selected = unit.name
                # Retry only the map tap. Never click the hero card again.
                return DeploymentAction("deploy", unit.name, count=1,
                                        corner_offset=pending.action.corner_offset+1)
            # Without explicit rejection and a visibly selected card, an
            # accepted hero input remains uncertain and must not be replayed.
            raise ClashAgentError(f"未能确认{unit.label}出场，已暂停；不会重复点击英雄卡片。")
        self.stalls[unit.name] += 1
        self.retry_generation += 1
        self.pending = None
        self.selected = ""
        if self.stalls[unit.name] >= 3:
            raise ClashAgentError(f"{unit.label}在多个方位均未确认投放进展，请检查视角或遮挡后继续。")
        return None

    def step(self, cards: tuple[CardObservation, ...], now: float, *, deployment_rejected=False,
             probing=False) -> DeploymentAction | None:
        observations = {card.name: card for card in cards}
        if self.pending is not None:
            action = self._verify_pending(observations, now, deployment_rejected)
            if action is not None or self.pending is not None or probing:
                return action
            # Confirmation already supplied a fresh bar observation. Continue
            # to the next unit/batch without an extra screenshot/poll cycle.
            # Probes need the runner to resolve their point before proceeding.
        # The same fixed order is used every battle. Heroes follow the opening
        # wave, rather than waiting for all 40 troops to finish.
        ordered = sorted(self.units, key=lambda u: (u.kind != "hero") if self.opening_verified else (u.kind == "hero"))
        if probing:
            ordered = [unit for unit in ordered if unit.kind == "troop"]
        for unit in ordered:
            if unit.name in self.done:
                continue
            card = observations.get(unit.name)
            if card is None or card.status == "unknown":
                self.skip_evidence.pop(unit.name, None)
                if self.selected == unit.name and now-self.selected_at < .7:
                    return None
                raise ClashAgentError(f"无法确认{unit.label}，请检查当前兵栏。")
            if card.status in ("empty", "deployed"):
                if unit.kind == "hero" and card.status == "deployed" and self.sent[unit.name] == 0:
                    self.skip_evidence[unit.name] = self.skip_evidence.get(unit.name, 0)+1
                    if self.skip_evidence[unit.name] < 2:
                        return None
                self.done.add(unit.name)
                if unit.kind == "troop":
                    self.opening_verified = True
                continue
            self.skip_evidence.pop(unit.name, None)
            if self.selected != unit.name:
                return self._select(unit, card)
            if now-self.selected_at < self.SELECTION_DELAY:
                return None
            budget = self.MAX_TROOP_TAPS if unit.kind == "troop" else 3
            remaining = budget-self.sent[unit.name]
            if remaining <= 0:
                raise ClashAgentError(f"{unit.label}达到本场投放预算，请检查兵栏。")
            # Never overrun a nearly empty slot and risk selecting another item.
            # Unknown numeric quantity falls back to one tap, not an estimate.
            count = min(self.BURST_SIZE, remaining, card.quantity) if unit.kind == "troop" and card.quantity is not None else 1
            if probing:
                if card.quantity is None:
                    raise ClashAgentError("试投前需要可靠识别剩余武神数量，未发送投放。")
                count = min(1, count)
            if count <= 0:
                self.done.add(unit.name)
                continue
            offset = self.units.index(unit)+self.stalls[unit.name]
            return DeploymentAction("deploy", unit.name, count=count, counter=card.counter,
                                    counter_pose=card.counter_pose, corner_offset=offset,
                                    probe=probing, quantity_before=card.quantity)
        return None

    def recheck_unused_heroes(self, cards: tuple[CardObservation, ...]) -> tuple[str, ...]:
        """Recover a skipped available hero without ever reselecting sent heroes."""
        observations = {card.name: card for card in cards}
        recovered = []
        for unit in self.units:
            card = observations.get(unit.name)
            if (unit.kind == "hero" and unit.name in self.done and self.sent[unit.name] == 0
                    and card is not None and card.status == "available"):
                self.done.remove(unit.name)
                self.skip_evidence.pop(unit.name, None)
                recovered.append(unit.label)
        return tuple(recovered)
