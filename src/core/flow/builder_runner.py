"""Builder battles with a separate deployment pass for each stage."""
from dataclasses import dataclass, field

from core.errors import ClashAgentError
from core.flow.camera import BuilderCameraNormalizer, CameraInterrupted
from core.flow.cycle_runner import CycleRunner
from core.flow.deployment_map import DeploymentMapSession, DropPoint


@dataclass
class PendingCard:
    slot: int
    action: str
    started: float
    point: DropPoint | None = None
    probe: bool = False
    skill_was_ready: bool = False


@dataclass
class BuilderStage:
    number: int
    camera: object = None
    points: DeploymentMapSession = field(default_factory=DeploymentMapSession)
    roster: tuple[int, ...] = ()
    pending: PendingCard | None = None
    placed: set[int] = field(default_factory=set)
    skills_sent: set[int] = field(default_factory=set)
    active_seen: bool = False
    waiting: bool = False
    observed_roster: tuple = ()


@dataclass
class BuilderCounts:
    sent: dict = field(default_factory=dict)
    confirmed: dict = field(default_factory=dict)


class BuilderCycleRunner(CycleRunner):
    HOME_STATE = "builder_base"
    HOME_LABEL = "建筑大师基地"
    RUN_LABEL = "夜世界"

    def __init__(self, definition, vision, device, control, emit, options, run_id):
        if (len(options.goals) != 1 or options.goals[0].resource != "gold"
                or options.goals[0].percent != 90):
            raise ClashAgentError("请先读取建筑大师金币最大容量。夜世界在金币达到 90% 时停止。")
        super().__init__(definition, None, device, control, emit, options, run_id,
                         cards=vision, resource_reader=vision.reader, camera_factory=BuilderCameraNormalizer)

    def _tap_card(self, version, x, y):
        return self.dispatch(version, round(x*self.frame.width/1280),
                             round(y*self.frame.height/720), fast=True)

    def _battle_frame(self, frame, stage):
        scene = self.recognizer.recognize(frame).state
        if scene == "builder_active":
            return True
        return scene == "builder_preview" and self.cards.second_preview(frame) == (stage.number == 2)

    def _align(self, stage, recognition):
        self.state("builder_camera", f"第 {stage.number} 阶段 · 校准地图", recognition)

        def notify(label, details, frame):
            self.frame = frame
            self.event("camera", label, details={"stage": stage.number, **details})

        stage.camera = self.camera_factory(
            self.device, self.control, self.checkpoint, notify,
            lambda frame: self._battle_frame(frame, stage),
        ).run(self.frame)
        self.frame = stage.camera.frame
        self.event("camera_ready", f"第 {stage.number} 阶段视角已固定", details={"stage": stage.number})

    def _observe_roster(self, stage, cards):
        key = tuple((slot, card.status) for slot, card in cards.items())
        slots = sorted(slot for slot in cards if slot)
        if (not slots or slots != list(range(1, slots[-1]+1))
                or any(card.status == "unknown" for card in cards.values())):
            stage.observed_roster = ()
            return False
        if key != stage.observed_roster:
            stage.observed_roster = key
            return False
        stage.roster = tuple(cards)
        self.event("deployment_ready", f"第 {stage.number} 阶段兵栏已确认",
                   details={"stage": stage.number, "slots": list(stage.roster)})
        return True

    def _pending_card(self, stage, cards, version, now, recognition):
        pending = stage.pending
        card = cards.get(pending.slot)
        expired = now-pending.started >= 1.4
        if card is None or card.status == "unknown":
            if expired:
                label = f"女巫 {pending.slot}" if pending.slot else "英雄"
                action = {"select": "选中", "drop": "投放", "skill": "技能释放"}[pending.action]
                raise BuilderEvidenceError(f"第 {stage.number} 阶段无法确认{label}的{action}结果，已暂停。")
            return
        details = {"stage": stage.number, "slot": card.slot, "unit": card.label}
        if pending.action == "select":
            if card.status == "available" and card.selected:
                stage.pending = None
                self._drop(stage, card, version, recognition)
            elif expired:
                raise BuilderEvidenceError(f"未能确认{card.label}选中。")
            return
        if pending.action == "skill":
            if card.status == "dead":
                self.event("warning", f"女巫 {card.slot} 已阵亡，技能结果无法确认", recognition, details)
                stage.pending = None
            elif card.status == "deployed" and not card.skill_ready:
                self.event("skill_verified", f"女巫 {card.slot} 技能已消耗", recognition, details)
                stage.pending = None
            elif expired:
                raise BuilderEvidenceError(f"未能确认女巫 {card.slot} 技能释放结果。")
            return
        if card.status in ("deployed", "dead"):
            if pending.probe:
                stage.points.resolve(pending.point, True)
                self.event("point_verified", f"第 {stage.number} 阶段落点已验证", recognition,
                           {**details, **pending.point.details()})
            stage.placed.add(card.slot)
            key = (stage.number, card.slot)
            self.deployment.confirmed[key] = 1
            self.event("deployment_verified", f"{card.label}已出场", recognition, details)
            stage.pending = None
            if card.slot and card.status == "deployed" and card.skill_ready:
                self._skill(stage, card, version, recognition)
            elif card.slot and card.status == "deployed" and pending.skill_was_ready:
                # The strip may appear one animation frame after the card lifts.
                stage.pending = PendingCard(card.slot, "skill_ready", now)
            return
        if expired:
            if not card.selected:
                raise BuilderEvidenceError(f"未能确认{card.label}是否已投放，已暂停。")
            if pending.probe:
                stage.points.resolve(pending.point, False)
            else:
                stage.points.reject((pending.point,))
            self.event("point_rejected", "落点未接收投放，尝试下一候选点", recognition,
                       {**details, **pending.point.details()})
            stage.pending = None

    def _skill(self, stage, card, version, recognition):
        if self._tap_card(version, card.x, 650):
            stage.skills_sent.add(card.slot)
            stage.pending = PendingCard(card.slot, "skill", self.control.active_time())
            self.event("skill_input", f"释放女巫 {card.slot} 蝙蝠技能", recognition,
                       {"stage": stage.number, "slot": card.slot})

    def _drop(self, stage, card, version, recognition):
        try:
            stage.camera.verify(self.frame)
            candidate = stage.points.next_probe()
            if candidate is None:
                proven = stage.points.require_points()
                point = proven[len(stage.placed) % len(proven)]
            else:
                point = candidate
        except ClashAgentError as exc:
            raise BuilderEvidenceError(str(exc)) from exc
        if self._tap_card(version, point.x, point.y):
            stage.pending = PendingCard(card.slot, "drop", self.control.active_time(), point,
                                        candidate is not None, card.skill_ready)
            key = (stage.number, card.slot)
            self.deployment.sent[key] = self.deployment.sent.get(key, 0)+1
            self.event("input", f"第 {stage.number} 阶段投放{card.label}", recognition,
                       {"stage": stage.number, "slot": card.slot, "probe": candidate is not None,
                        **point.details()})

    def _deploy(self, stage, recognition, version, now):
        cards = self.cards.cards(self.frame, tracked_slots=stage.roster)
        if not stage.roster:
            if not self._observe_roster(stage, cards):
                return False
        if stage.pending:
            if stage.pending.action == "skill_ready":
                card = cards.get(stage.pending.slot)
                if card and card.status == "deployed" and card.skill_ready:
                    self._skill(stage, card, version, recognition)
                elif card and card.status == "dead":
                    stage.pending = None
                elif now-stage.pending.started >= 1.4:
                    raise BuilderEvidenceError("女巫已出场，但技能按钮未能确认。")
            else:
                self._pending_card(stage, cards, version, now, recognition)
            return True

        # An interruption between deployment and the ability tap leaves
        # the used slot in place. Re-read its current ability strip on resume.
        for slot in stage.placed:
            card = cards.get(slot)
            if slot and slot not in stage.skills_sent and card and card.status == "deployed" and card.skill_ready:
                self._skill(stage, card, version, recognition)
                return True
        available = []
        for slot in stage.roster:
            if slot in stage.placed:
                continue
            card = cards.get(slot)
            if card is None or card.status == "unknown":
                return False
            if card.status == "available":
                available.append(card)
            elif card.status == "deployed":
                raise BuilderEvidenceError("检测到脚本未投放的部队，已暂停以确认兵栏。")
        if not available:
            stage.waiting = True
            self.state("builder_wait", f"第 {stage.number} 阶段投放完成 · 等待结算或转场", recognition)
            return True
        # Night Witches test the four sides first. Place the hero once
        # those sides are confirmed, before the remaining witches.
        probe = stage.points.probing
        card = min(available, key=lambda c: (not bool(c.slot) if probe else bool(c.slot), c.slot))
        if card.selected:
            self._drop(stage, card, version, recognition)
        elif self._tap_card(version, card.x, 650):
            stage.pending = PendingCard(card.slot, "select", self.control.active_time())
        return True

    def battle(self, *, initial_frame=None):
        self.deployment = BuilderCounts()
        stage = BuilderStage(1)
        returned = False
        result_at = None
        stable = 0
        previous = None
        version_seen = None
        progress_at = self.control.active_time()
        stage_started = progress_at
        while True:
            version = self.checkpoint()
            now = self.control.active_time()
            if version != version_seen:
                stable, previous = 0, None
                progress_at = stage_started = now
                if stage.pending:
                    stage.pending.started = now
                version_seen = version
            self.frame = initial_frame if initial_frame is not None else self.device.capture()
            initial_frame = None
            if self.checkpoint() != version:
                continue
            recognition = self.recognizer.recognize(self.frame)
            name = recognition.state
            second = name == "builder_preview" and self.cards.second_preview(self.frame)
            key = name, second
            stable = stable+1 if key == previous else 1
            previous = key
            self.event("observation", recognition.label, recognition, {"stage": stage.number})
            if stable < self.definition.stable_frames:
                self.control.wait(.06)
                continue
            if name == "builder_result" and not returned:
                hit = next(h for h in recognition.hits if h.name == "return")
                region = hit.region
                self.state("builder_result", "夜世界结算 · 回营", recognition)
                if self.dispatch(version, region.x+region.width//2, region.y+region.height//2):
                    returned, result_at = True, now
                    self.event("action", "已点击回营", recognition, {"stage": stage.number})
            elif name == self.HOME_STATE and returned:
                self.state("builder_base", "已确认回到建筑大师基地", recognition)
                return
            elif returned:
                if now-result_at >= 20:
                    self.control.pause("已点击回营，尚未确认建筑大师基地。")
            elif name in ("builder_preview", "builder_active"):
                if second and stage.number == 1:
                    if not stage.active_seen:
                        self.control.pause("未确认第一阶段过程，请检查夜世界战斗进度。")
                        continue
                    stage = BuilderStage(2)
                    progress_at = stage_started = now
                    self.event("stage_changed", "进入第二阶段，重新识别幸存部队和援军", recognition,
                               {"stage": 2})
                elif name == "builder_preview" and stage.number == 2 and not second:
                    self.control.pause("第二阶段备战画面未能确认。")
                    continue
                stage.active_seen |= name == "builder_active"
                if now-stage_started >= 205:
                    self.control.pause(f"夜世界第 {stage.number} 阶段等待超时。")
                    continue
                if stage.waiting:
                    progress_at = now
                elif stage.camera is None:
                    try:
                        self._align(stage, recognition)
                        progress_at = self.control.active_time()
                    except CameraInterrupted:
                        continue
                    except ClashAgentError as exc:
                        self.control.pause(str(exc))
                else:
                    self.state("builder_deploy", f"第 {stage.number} 阶段 · 四侧投放与女巫技能", recognition)
                    # Input transport errors are terminal to avoid replaying an
                    # operation that may have been delivered.
                    try:
                        if self._deploy(stage, recognition, version, now):
                            progress_at = now
                    except BuilderEvidenceError as exc:
                        self.control.pause(str(exc))
                    if now-progress_at >= 8:
                        self.control.pause("无法确认夜世界兵栏，请检查女巫阵容或遮挡。")
            elif name == "inactive_dialog":
                self.control.pause(self.definition.node(name).message)
            elif now-progress_at >= 15:
                self.control.pause("夜世界画面识别超时，请检查界面。")
            self.control.wait(.20 if stage.waiting or returned else .06)


class BuilderEvidenceError(ClashAgentError):
    """Visual evidence is insufficient to send a further input."""
