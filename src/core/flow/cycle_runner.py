from dataclasses import replace
from datetime import datetime
from time import monotonic, perf_counter
from uuid import uuid4

from core.errors import ClashAgentError
from core.flow.control import FlowStopped, RunControl
from core.flow.deployment import CardDetector, DeploymentController
from core.resources import FarmOptions, ResourceGuard, ResourceTargetReached, RESOURCE_LABELS
from core.flow.models import FlowEvent, FlowResult
from core.flow.deployment_map import DeploymentMapSession
from core.flow.recognizer import StateRecognizer
from core.flow.runner import TaskRunner
from core.flow.camera import CameraNormalizer, CameraInterrupted


class CycleRunner:
    """Search -> deploy -> observe settlement -> verify village -> next cycle."""
    PREPARATION_SECONDS = 25.0
    DEPLOY_POLL_INTERVAL = .06
    HOME_STATE = "village"
    HOME_LABEL = "村庄"
    RUN_LABEL = "常规战"

    def __init__(self, definition, plan, device, control: RunControl, emit,
                 options: FarmOptions | None = None, run_id=None, recognizer=None, cards=None, map_locator=None,
                 resource_reader=None, camera_factory=CameraNormalizer):
        self.definition, self.plan, self.device = definition, plan, device
        self.control, self.emit = control, emit
        self.options = options or FarmOptions()
        self.options.validate()
        self.run_id = run_id or uuid4().hex
        self.recognizer = recognizer or StateRecognizer(definition)
        self.cards = cards or CardDetector(plan, resource_reader)
        self.map_locator = map_locator
        self.camera_factory = camera_factory
        self.phase, self.node = "starting", ""
        self.completed = 0
        self.frame = None
        self.deployment = None
        self.message = ""
        self.resource_reader = resource_reader
        self.resource_guard = ResourceGuard()
        self.resource_version = None
        self.resource_started = 0.0
        self.checking_resources = False
        self.readings = ()
        if self.options.goals and resource_reader is None:
            raise ClashAgentError("启用资源停止条件时需要数字识别资源。")

    def event(self, kind, message="", recognition=None, details=None):
        if kind == "terminal":
            self.message = message
        count = sum(self.deployment.sent.values()) if self.deployment else 0
        batches = sum(self.deployment.confirmed.values()) if self.deployment else 0
        cycle = self.completed if kind == "cycle_completed" or (kind == "terminal" and self.phase == "completed") else self.completed+1
        self.emit(FlowEvent(kind, self.phase, self.node, message, self.frame, recognition,
                            self.run_id, cycle, self.completed, count, batches, self.readings, details or {}))

    def check_resources(self, frame, version):
        if not self.options.goals:
            return True
        self.frame = frame
        if not self.checking_resources or version != self.resource_version:
            self.resource_guard.reset()
            self.resource_started = self.control.active_time()
            self.resource_version = version
            self.checking_resources = True
            self.state("resource_check", f"检查{self.HOME_LABEL}资源储量", None)
        readings = self.resource_reader.read(frame, self.options.goals)
        if readings:
            self.readings = readings
            self.event("resources", f"读取{self.HOME_LABEL}资源")
        if not self.resource_guard.accept(readings):
            if self.control.active_time()-self.resource_started >= 8:
                self.control.pause(f"无法连续确认{self.HOME_LABEL}资源数字，请检查容量设置或画面后继续")
            return False
        reached = next((r for r in readings if r.reached), None)
        self.checking_resources = False
        if reached:
            raise ResourceTargetReached(
                f"{RESOURCE_LABELS[reached.resource]}已达 {reached.percent:.1f}%（目标 {reached.target_percent}%），停止刷资源")
        return True

    def checkpoint(self):
        def paused(reason):
            self.phase = "paused"
            self.event("phase", reason)
        version = self.control.checkpoint(paused)
        if self.phase in ("starting", "paused"):
            self.phase = "running"
            self.event("phase", "重新确认当前战斗画面与投放进度")
        return version

    def state(self, name, label, recognition):
        if self.node != name:
            self.node = name
            self.event("state", label, recognition)

    def dispatch(self, version, x, y, *, fast=False):
        if not 0 <= x < self.frame.width or not 0 <= y < self.frame.height:
            raise ClashAgentError("投放坐标超出实际画面。")
        self.device.ensure_foreground()
        if fast and hasattr(self.device, "fast_tap"):
            return self.device.fast_tap(x, y, self.frame, self.control, version)
        return self.control.dispatch(version, lambda: self.device.tap(x, y))

    def battle(self, camera_reference=None, initial_frame=None):
        self.deployment = DeploymentController(self.plan)
        if hasattr(self.cards, "reset"):
            self.cards.reset()
        prepared = not hasattr(self.cards, "prepare")
        prepare_started = None
        map_locator = self.map_locator or DeploymentMapSession()
        camera_ref = camera_reference
        camera_started = camera_reference is not None
        pending_drop = None
        result_seen = False
        returned_at = None
        return_attempts = 0
        return_last = 0
        started = self.control.active_time()
        unknown_since = started
        stable_name, stable_count = "", 0
        version_seen = None
        last_confirmed = 0
        popup_pending = {}
        last_hero_audit = float("-inf")
        ready_frame = initial_frame
        preparation_started = monotonic()
        while True:
            version = self.checkpoint()
            if version != version_seen:
                self.deployment.resumed(self.control.active_time())
                # Preserve accepted inputs and their verification state across pause.
                # Active time already excludes time spent paused.
                stable_name, stable_count = "", 0
                unknown_since = self.control.active_time()
                if version_seen is not None:
                    ready_frame = None
                    started = self.control.active_time()
                    popup_pending.clear()
                    if result_seen:
                        returned_at, return_attempts = None, 0
                version_seen = version
            # Reading a frame needs no dumpsys call. Every input path checks
            # foreground immediately before selection, a burst or a gesture.
            # Search and final camera stabilization already confirmed two
            # frames. Reuse their last fresh frame instead of starting over.
            reused = (ready_frame is not None and 0 <= (
                datetime.now(ready_frame.captured_at.tzinfo)-ready_frame.captured_at).total_seconds() <= .75)
            capture_start = perf_counter()
            self.frame = ready_frame if reused else self.device.capture()
            capture_ms = (perf_counter()-capture_start)*1000
            ready_frame = None
            if self.checkpoint() != version:
                continue
            recognize_start = perf_counter()
            recognition = self.recognizer.recognize(self.frame)
            recognize_ms = (perf_counter()-recognize_start)*1000
            self.event("observation", recognition.label, recognition)
            if capture_ms > 1000 or recognize_ms > 250:
                self.event("timing", "战斗画面处理耗时", details={
                    "capture_ms": capture_ms, "recognize_ms": recognize_ms, "frame_reused": reused})
            now = self.control.active_time()
            name = recognition.state
            if (not any(self.deployment.sent.values()) and not result_seen
                    and name not in ("battle_result", "village")
                    and monotonic()-preparation_started >= self.PREPARATION_SECONDS):
                self.control.pause("下兵准备超过 25 秒预算，已阻止继续等待；请停止任务后检查耗时日志并重新开始。")
                continue
            if not name:
                stable_name, stable_count = "", 0
                if now - unknown_since > 15:
                    self.control.pause("战斗画面识别超时；请检查弹窗或游戏状态后继续")
                self.control.wait(self.definition.poll_interval)
                continue
            stable_count = stable_count+1 if stable_name == name else 1
            if reused and name in ("opponent_found", "battle_active", "battle_active_red"):
                stable_count = self.definition.stable_frames
            stable_name = name
            if stable_count < self.definition.stable_frames:
                self.control.wait(min(self.definition.poll_interval, self.DEPLOY_POLL_INTERVAL)
                                  if camera_ref is not None else self.definition.poll_interval)
                continue
            unknown_since = now
            if self.definition.node(name).action == "pause":
                self.control.pause(self.definition.node(name).message)
            elif name == "rating_prompt":
                node = self.definition.node(name)
                self.state(name, "关闭评分提示（稍后）", recognition)
                first, last, attempts = popup_pending.get(name, (now, 0, 0))
                if now-first > node.timeout:
                    self.control.pause("评分提示未关闭，请检查后继续")
                elif attempts < node.max_attempts and (attempts == 0 or now-last >= node.retry_interval):
                    r = next(h.region for h in recognition.hits if h.name == node.target)
                    if self.dispatch(version, r.x+r.width//2, r.y+r.height//2):
                        popup_pending[name] = (first, now, attempts+1)
                        self.event("action", "评分提示：点击稍后，等待村庄确认", recognition)
                        stable_name, stable_count = "", 0
            elif name == "battle_result":
                result_seen = True
                self.state("battle_result", "战斗已结束 · 结算", recognition)
                if returned_at is None or (now-return_last >= 3 and return_attempts < 2):
                    hit = next(h for h in recognition.hits if h.name == "result_return")
                    r = hit.region
                    if self.dispatch(version, r.x+r.width//2, r.y+r.height//2):
                        returned_at = now if returned_at is None else returned_at
                        return_last = now
                        return_attempts += 1
                        self.state("returning", "正在返回村庄", recognition)
                        self.event("action", "点击回营，等待新画面确认村庄", recognition)
                        stable_name, stable_count = "", 0
                elif now-returned_at > 20:
                    self.control.pause("回营后未能确认村庄，请检查游戏后继续")
            elif name == "village":
                if not result_seen:
                    self.control.pause("未识别到本场结算，不能将村庄画面计为一轮完成")
                else:
                    self.state("village", "已验证返回主世界村庄", recognition)
                    self.event("verified", "结算与回营均已确认", recognition)
                    return
            elif name == "matchmaking":
                if result_seen:
                    self.state("returning", "返回村庄的云层过渡", recognition)
                    if returned_at is not None and now-returned_at > 20:
                        self.control.pause("返回村庄等待超时")
                elif now-started > 240:
                    self.control.pause("战斗流程超过等待预算，请检查后继续")
            elif name in ("opponent_found", "battle_active", "battle_active_red"):
                if result_seen:
                    self.control.pause("结算后出现新的战斗画面，已暂停以避免重复投放")
                elif now-started > 240:
                    self.control.pause("等待战斗结算超时，请检查后继续")
                elif camera_ref is None:
                    if name != "opponent_found" and not camera_started:
                        self.control.pause("请从村庄或对手预览开始，以便在下兵前统一地图视角。")
                        continue
                    camera_started = True
                    self.state("camera_normalize", "统一缩放并固定地图位置", recognition)
                    def notify(label, details, frame):
                        self.frame = frame
                        self.event("camera", label, details=details)
                    try:
                        frame_check = lambda frame: self.recognizer.recognize(frame).state in (
                            "opponent_found", "battle_active", "battle_active_red")
                        camera_ref = self.camera_factory(self.device, self.control, self.checkpoint, notify, frame_check).run(self.frame)
                        self.frame = camera_ref.frame
                        self.event("camera_ready", "视角固定完成，准备逐点试投", details=map_locator.details())
                        ready_frame = self.frame
                        stable_name, stable_count = "", 0
                        started = self.control.active_time()
                    except CameraInterrupted:
                        continue
                    except ClashAgentError as exc:
                        self.control.pause(str(exc))
                    continue
                elif self.deployment.finished:
                    if now-last_hero_audit >= 2:
                        last_hero_audit = now
                        recovered = self.deployment.recheck_unused_heroes(self.cards.observe(self.frame))
                        if recovered:
                            self.event("warning", f"复查发现未投放的{'、'.join(recovered)}，重新确认投放", recognition)
                    if self.deployment.finished:
                        self.state("battle_wait", "投放完成 · 等待战斗结束", recognition)
                else:
                    self.state("point_probe" if map_locator.probing else "battle_deploy",
                               "逐点试投验证四侧落点" if map_locator.probing else "复用合法落点投放武神与英雄", recognition)
                    if not prepared:
                        if prepare_started is None:
                            prepare_started = now
                        plan = self.cards.prepare(self.frame)
                        if plan is None:
                            if now-prepare_started > 12:
                                self.control.pause("无法稳定确认完整兵栏，请检查画面后继续")
                            self.control.wait(min(self.definition.poll_interval, self.DEPLOY_POLL_INTERVAL))
                            continue
                        self.deployment = DeploymentController(plan)
                        prepared = True
                        count = sum(unit.kind == "hero" for unit in plan.units)
                        self.event("deployment_ready", f"已扫描 {count} 个英雄卡槽，按从左到右投放可用英雄", recognition)
                    previous_retries = self.deployment.retry_generation
                    before_pending = self.deployment.pending
                    previous_confirmed = sum(self.deployment.confirmed.values())
                    try:
                        observations = self.cards.observe(self.frame)
                        troops = [c for c in observations if any(u.name == c.name and u.kind == "troop" for u in self.deployment.units)]
                        if (map_locator.probing and self.deployment.pending is None and troops
                                and all(c.status == "empty" for c in troops)):
                            map_locator.finish_probing()
                        if not map_locator.probing:
                            map_locator.require_points()
                        action = self.deployment.step(observations, now,
                            deployment_rejected=self.cards.deployment_rejected(self.frame), probing=map_locator.probing)
                    except ClashAgentError as exc:
                        self.control.pause(str(exc))
                        action = None
                    confirmed = sum(self.deployment.confirmed.values())
                    if pending_drop is not None and before_pending is not None and self.deployment.pending is None:
                        if before_pending.sent:
                            success = confirmed > previous_confirmed
                            if pending_drop["probe"] is not None:
                                point = pending_drop["probe"]
                                map_locator.resolve(point, success)
                                after = next(c for c in observations if c.name == before_pending.action.unit)
                                details = {**point.details(), "quantity_before": before_pending.action.quantity_before,
                                           "quantity_after": 0 if after.status == "empty" else after.quantity,
                                           "success": success, **map_locator.details()}
                                self.event("point_verified" if success else "point_rejected",
                                           f"{('试投成功' if success else '弃用无效点')}：{point.x}, {point.y}", recognition, details)
                            elif self.deployment.retry_generation > previous_retries:
                                map_locator.reject(pending_drop["points"])
                                self.event("point_rejected", "撤销本批无进展的落点，只使用其余已验证点", recognition, map_locator.details())
                        pending_drop = None
                    if confirmed != last_confirmed:
                        last_confirmed = confirmed
                        self.event("deployment_verified", f"已确认 {confirmed} 批兵力变化或英雄出场", recognition)
                    if action:
                        if action.kind == "deploy":
                            try:
                                camera_ref.verify(self.frame)
                                probe = map_locator.next_probe() if action.probe else None
                                points = (probe,) if probe is not None else map_locator.require_points()
                            except ClashAgentError as exc:
                                self.control.pause(str(exc))
                                continue
                            # A transport failure may have delivered an input.
                            # Let it terminate the run; never resume/replay it.
                            first_input = not any(self.deployment.sent.values())
                            sent_points = self.send_burst(version, action, points, recognition)
                            if first_input and sent_points:
                                self.event("timing", "首次试投已发送", details={
                                    "preparation_ms": (monotonic()-preparation_started)*1000})
                            pending_drop = {"probe": probe, "points": sent_points}
                        else:
                            x = round(action.x * self.frame.width / self.plan.width)
                            y = round(action.y * self.frame.height / self.plan.height)
                            if not self.dispatch(version, x, y, fast=True):
                                continue
                            self.deployment.select_accepted(action, self.control.active_time())
                            unit = next(u for u in self.deployment.units if u.name == action.unit)
                            self.event("deployment_action", f"选择{unit.label}", recognition)
            else:
                self.control.pause(f"战斗阶段出现非预期界面：{recognition.label}")
            deploying = camera_ref is not None and not self.deployment.finished and not result_seen
            self.control.wait(min(self.definition.poll_interval, self.DEPLOY_POLL_INTERVAL)
                              if deploying else self.definition.poll_interval)

    def send_burst(self, version, action, points, recognition):
        start = perf_counter()
        if not points:
            raise ClashAgentError("缺少可用的四侧投放点。")
        self.device.ensure_foreground()
        self.deployment.begin_burst(action, self.control.active_time())
        sent = 0
        sent_points = []
        for index in range(action.count):
            point = points[(index+action.corner_offset) % len(points)]
            rx, ry = point.x, point.y
            x = round(rx*self.frame.width/self.plan.width)
            y = round(ry*self.frame.height/self.plan.height)
            if not 0 <= x < self.frame.width or not 0 <= y < self.frame.height:
                raise ClashAgentError("投放点超出画面。")
            # Every tap passes the pause/stop gate; a burst is never submitted
            # as one uninterruptible Android shell script.
            if hasattr(self.device, "fast_tap"):
                accepted = self.device.fast_tap(x, y, self.frame, self.control, version)
            else:
                accepted = self.control.dispatch(version, lambda x=x, y=y: self.device.tap(x, y))
            if not accepted:
                break
            self.deployment.tap_accepted()
            sent += 1
            sent_points.append(point)
            self.event("input", f"{'试投' if action.probe else '投放'} {action.unit}：{rx}, {ry}", recognition,
                       {**point.details(), "screen": [x, y], "unit": action.unit, "probe": action.probe,
                        "quantity_before": action.quantity_before})
            self.control.wait(self.deployment.TAP_INTERVAL)
        self.deployment.end_burst(self.control.active_time())
        unit = next(u for u in self.deployment.units if u.name == action.unit)
        self.event("deployment_action", f"{unit.label}：本批发送 {sent} 次四侧投放，等待兵栏确认", recognition,
                   {"sent": sent, "elapsed_ms": (perf_counter()-start)*1000,
                    "input_mode": "persistent_touch" if hasattr(self.device, "fast_tap") else "adb"})
        return tuple(sent_points)

    def run(self):
        self.event("phase", f"{self.RUN_LABEL}循环任务准备中")
        try:
            while True:
                self.checkpoint()
                self.deployment = None

                def forward(event):
                    self.frame = event.frame or self.frame
                    self.node = event.node
                    completed_search = event.kind == "terminal" and event.phase == "completed"
                    self.phase = "running" if completed_search else event.phase
                    self.emit(replace(event, kind="search_complete" if completed_search else event.kind,
                                      phase=self.phase, cycle=self.completed+1, cycles_completed=self.completed))

                search = TaskRunner(self.definition, self.device, self.control, forward,
                                    self.recognizer, self.run_id, before_village=self.check_resources,
                                    home_state=self.HOME_STATE).run()
                if search.phase != "completed":
                    return FlowResult(search.phase, search.node, search.message, self.run_id, self.completed)
                self.battle(initial_frame=self.frame)
                self.completed += 1
                self.event("cycle_completed", f"已完成 {self.completed} 轮，已回到{self.HOME_LABEL}")
        except ResourceTargetReached as exc:
            self.phase = "completed"
            self.node = self.HOME_STATE
            self.event("terminal", str(exc))
        except FlowStopped as exc:
            self.phase = "failed" if exc.reason else "stopped"
            self.event("terminal", exc.reason or "用户停止；未发送后续输入")
        except Exception as exc:
            self.phase = "failed"
            self.event("terminal", str(exc) or type(exc).__name__)
        return FlowResult(self.phase, self.node, self.message, self.run_id, self.completed)
