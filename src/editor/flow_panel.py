"""A single farming task: fixed strategy, resource targets and live status."""
from collections.abc import Callable

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QProgressBar,
    QPushButton, QScrollArea, QSpinBox, QSplitter, QVBoxLayout, QWidget,
)

from core.errors import ClashAgentError
from core.flow.control import RunControl
from core.flow.models import FlowEvent, FlowResult
from core.resources import RESOURCE_LABELS
from editor.preview.canvas import PreviewCanvas
from editor.workers import FlowTask, CapacityTask
from infrastructure.farm_settings import FarmPreferences, FarmSettingsStore
from infrastructure.settings import Settings

PHASES = {"idle": "就绪", "starting": "准备中", "running": "运行中", "pausing": "正在暂停",
          "paused": "已暂停", "stopping": "正在停止", "stopped": "已停止",
          "completed": "目标已达成", "failed": "任务异常", "reading_capacity": "读取容量"}


class FlowPanel(QWidget):
    active_changed = Signal(bool)
    message = Signal(str)

    def __init__(self, prepare: Callable[[], Settings], store: FarmSettingsStore | None = None, parent=None,
                 *, builder=False):
        super().__init__(parent)
        self.prepare, self.store = prepare, store or FarmSettingsStore()
        self.builder = builder
        self.settings = None
        self.session_key = None
        self.task = None
        self.control = None
        self.capacity_task = None
        self.capacity_control = None
        self.capacity_error = ""
        self.phase = "idle"
        self.result = None
        self.load_error = ""
        try:
            preferences = self.store.load()
        except ClashAgentError as exc:
            preferences = FarmPreferences()
            self.load_error = str(exc)
        self._build_ui(preferences)
        self._update_controls()

    @property
    def busy(self):
        return self.task is not None or self.capacity_task is not None

    def _build_ui(self, preferences):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        toolbar = QHBoxLayout()
        self.start_button = QPushButton("开始刷资源")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(self._toggle_run)
        self.stop_button = QPushButton("停止")
        self.stop_button.clicked.connect(self.stop)
        self.phase_label = QLabel("就绪")
        self.phase_label.setObjectName("status")
        toolbar.addWidget(self.start_button)
        toolbar.addWidget(self.stop_button)
        toolbar.addStretch()
        toolbar.addWidget(self.phase_label)
        layout.addLayout(toolbar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        controls = QWidget()
        controls.setMinimumWidth(300)
        controls.setMaximumWidth(380)
        side = QVBoxLayout(controls)
        side.setContentsMargins(0, 0, 10, 0)
        goals = QGroupBox("资源停止条件")
        goal_layout = QVBoxLayout(goals)
        threshold = QHBoxLayout()
        self.percent = QSpinBox(goals)
        self.percent.setRange(10, 100)
        self.percent.setSuffix(" %")
        self.percent.setValue(preferences.percent)
        if self.builder:
            self.percent.setRange(90, 90)
            self.percent.hide()
        else:
            threshold.addWidget(QLabel("任一所选资源达到"))
            threshold.addWidget(self.percent)
            threshold.addWidget(QLabel("时停止"))
            threshold.addStretch()
            goal_layout.addLayout(threshold)
        self.targets = {}
        self.target_labels = {}
        self.cached_capacities = dict(preferences.capacities)
        self.capacity_labels = {}
        self.capacity_rows = {}
        self.bars = {}
        self.read_capacity_button = QPushButton("读取最大容量")
        self.read_capacity_button.clicked.connect(self.read_capacities)
        goal_layout.addWidget(self.read_capacity_button)
        for name, label in RESOURCE_LABELS.items():
            if self.builder and name != "gold":
                continue
            row = QHBoxLayout()
            checkbox = QCheckBox(label, goals)
            checkbox.setChecked(self.builder or name in preferences.enabled)
            target_label = QLabel()
            target_label.setObjectName("hint")
            if self.builder:
                checkbox.hide()
                row.addWidget(QLabel("建筑大师金币"))
            else:
                row.addWidget(checkbox)
            row.addStretch()
            row.addWidget(target_label)
            goal_layout.addLayout(row)
            capacity_row = QWidget()
            capacity_layout = QHBoxLayout(capacity_row)
            capacity_layout.setContentsMargins(0, 0, 0, 0)
            capacity = QLabel()
            capacity.setObjectName("hint")
            capacity.setWordWrap(True)
            capacity_layout.addWidget(capacity, 1)
            goal_layout.addWidget(capacity_row)
            progress = QProgressBar()
            progress.setRange(0, 100)
            progress.setValue(0)
            progress.setFormat("等待村庄资源读取")
            goal_layout.addWidget(progress)
            self.targets[name], self.capacity_labels[name], self.bars[name] = checkbox, capacity, progress
            self.target_labels[name], self.capacity_rows[name] = target_label, capacity_row
        note = QLabel("仓库升级后可重新读取最大容量。"
                      if self.builder else
                      "容量只在点击按钮时读取并保存；仓库升级后可重新读取。\n没有已保存容量的资源不参与停止判断。\n任一有效资源达标即停止；无有效条件时持续运行。")
        note.setWordWrap(True)
        note.setObjectName("hint")
        goal_layout.addWidget(note)
        side.addWidget(goals)

        status = QGroupBox("当前运行")
        form = QFormLayout(status)
        self.state_label = QLabel("等待开始")
        self.cycle_label = QLabel("已完成 0 轮")
        self.deployment_label = QLabel("尚未投放")
        for caption, widget in (("当前步骤", self.state_label), ("运行进度", self.cycle_label),
                                ("本场投放", self.deployment_label)):
            widget.setWordWrap(True)
            form.addRow(caption, widget)
        self.detail_label = QLabel("先连接 MuMu，手动进入建筑大师基地并配好女巫。"
                                   if self.builder else "先连接已打开的 MuMu，进入主世界村庄。")
        self.detail_label.setWordWrap(True)
        self.detail_label.setMinimumHeight(42)
        form.addRow(self.detail_label)
        # Keep live state and counters visible without scrolling past settings.
        side.insertWidget(0, status)
        strategy = QLabel(
            "预先配置女巫。每阶段沿四侧投放，女巫出场后释放可用技能。"
            "进入第二阶段后重新识别幸存兵力和援军。"
            if self.builder else
            "固定策略 · 四角快投\n统一视角后，四侧各试投一名武神。\n"
            "确认成功落点后，下全部可用英雄，剩余武神快速轮流铺到四角。\n"
            "忽略法术、攻城机器和英雄技能。\n暂停仅控制脚本，游戏本身仍继续。")
        strategy.setWordWrap(True)
        strategy.setObjectName("hint")
        side.addWidget(strategy)
        self.run_label = QLabel("")
        self.run_label.setWordWrap(True)
        self.run_label.setObjectName("hint")
        self.run_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        side.addWidget(self.run_label)
        side.addStretch()
        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setMinimumWidth(325)
        settings_scroll.setMaximumWidth(405)
        settings_scroll.setWidget(controls)
        splitter.addWidget(settings_scroll)

        preview = QGroupBox("实时画面")
        preview_layout = QVBoxLayout(preview)
        self.canvas = PreviewCanvas()
        preview_layout.addWidget(self.canvas, 1)
        self.frame_label = QLabel("启动任务后显示画面")
        self.frame_label.setObjectName("hint")
        preview_layout.addWidget(self.frame_label)
        splitter.addWidget(preview)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([320, 850])
        layout.addWidget(splitter, 1)
        for checkbox in self.targets.values():
            checkbox.toggled.connect(self._update_controls)
            checkbox.toggled.connect(self._sync_targets)
        self.percent.valueChanged.connect(self._sync_targets)
        self._sync_targets()

    def _sync_targets(self, *_):
        percent = self.percent.value()
        for name, checkbox in self.targets.items():
            selected = checkbox.isChecked()
            capacity = self.cached_capacities[name]
            missing_label = "待读取容量" if self.builder else "条件未生效"
            self.target_labels[name].setText(
                (f"达到 {percent}% 时停止" if capacity else missing_label) if selected else "未选择")
            missing_capacity = "启动前读取金币最大容量" if self.builder else "未读取容量 · 此项不参与停止判断"
            self.capacity_labels[name].setText(
                f"已保存最大容量：{capacity:,}" if capacity else missing_capacity)
            self.capacity_rows[name].setVisible(selected or capacity > 0)
            self.bars[name].setVisible(selected and capacity > 0)
            if self.task is None:
                self.bars[name].setValue(0)
                self.bars[name].setFormat(f"目标 {percent}% · 等待读取当前储量")
                self.bars[name].setToolTip("")

    def preferences(self) -> FarmPreferences:
        return FarmPreferences(self.percent.value(),
                               dict(self.cached_capacities),
                               tuple(name for name, checkbox in self.targets.items() if checkbox.isChecked()))

    def set_session(self, settings: Settings | None, key=None):
        if self.busy and (settings != self.settings or key != self.session_key):
            self.stop(reason="设备连接或目标实例已改变，任务已终止；重新连接后需手动开始")
        self.settings, self.session_key = settings, key
        self._update_controls()

    @Slot()
    def _toggle_run(self):
        if self.task is None:
            self.start()
        elif self.phase == "paused":
            self.resume()
        elif self.phase in ("starting", "running"):
            self.pause()

    @Slot()
    def start(self):
        if self.busy or self.settings is None:
            return
        try:
            preferences = self.preferences()
            options = preferences.options()
            if self.builder and not options.goals:
                raise ClashAgentError("请先点击「读取最大容量」，保存建筑大师金币容量后再开始。")
            settings = self.prepare()
            self.store.save(preferences)
        except ClashAgentError as exc:
            self.detail_label.setText(str(exc))
            self.message.emit(str(exc))
            return
        self.control = RunControl()
        self.result = None
        self.phase = "starting"
        self.state_label.setText("准备刷资源")
        self.cycle_label.setText("已完成 0 轮")
        self.deployment_label.setText("尚未投放")
        for bar in self.bars.values():
            bar.setValue(0)
            bar.setFormat(f"目标 {self.percent.value()}% · 等待读取当前储量")
        self.task = FlowTask(settings, self.control, self, options=options, builder=self.builder)
        self.run_label.setText(f"运行记录：logs/runs/{self.task.run_id}")
        self.task.progress.connect(self.on_event)
        self.task.completed.connect(self.on_result)
        self.task.finished.connect(self._finished)
        self.detail_label.setText("准备识别资源与截图；首次采集需要数秒。")
        missing = [RESOURCE_LABELS[n] for n in preferences.enabled if not preferences.capacities[n]]
        if missing:
            self.message.emit(f"{'、'.join(missing)}未保存最大容量，其停止条件本次不生效。")
        if not options.goals:
            self.message.emit("本次没有有效资源停止条件，将持续运行，直到手动停止。")
        self.active_changed.emit(True)
        self._update_controls()
        self.task.start()

    @Slot()
    def pause(self):
        if self.task is not None and self.phase in ("starting", "running"):
            self.control.pause()
            self.phase = "pausing"
            self.detail_label.setText("已阻止后续输入，正在等待当前操作结束。")
            self._update_controls()

    @Slot()
    def resume(self):
        if self.task is not None and self.phase == "paused" and self.settings is not None:
            self.control.resume()
            self.phase = "running"
            self.detail_label.setText("重新确认画面与未完成的投放批次。")
            self._update_controls()

    def stop(self, checked=False, *, reason=""):
        if self.capacity_task is not None:
            self.capacity_control.stop(reason)
            self.phase = "stopping"
            self.detail_label.setText(reason or "正在停止容量读取。")
            self._update_controls()
        if self.task is not None:
            self.control.stop(reason)
            self.phase = "stopping"
            self.detail_label.setText(reason or "正在停止，等待当前操作退出。")
            self._update_controls()

    @Slot()
    def read_capacities(self):
        if self.busy or self.settings is None:
            return
        try:
            settings = self.prepare()
        except ClashAgentError as exc:
            self.detail_label.setText(str(exc))
            return
        self.capacity_error = ""
        self.capacity_control = RunControl()
        self.capacity_task = CapacityTask(settings, self.capacity_control, self, builder=self.builder)
        self.capacity_task.progress.connect(self._capacity_progress)
        self.capacity_task.succeeded.connect(self._capacity_succeeded)
        self.capacity_task.failed.connect(self._capacity_failed)
        self.capacity_task.finished.connect(self._capacity_finished)
        self.phase = "reading_capacity"
        self.state_label.setText("读取资源最大容量")
        self.detail_label.setText("只查看村庄资源说明，不开始搜索或战斗。")
        self.active_changed.emit(True)
        self._update_controls()
        self.capacity_task.start()

    @Slot(object)
    def _capacity_progress(self, event):
        if event.frame is not None:
            self.canvas.set_frame(event.frame)
            self.frame_label.setText(f"{event.frame.width} × {event.frame.height}")
        if event.kind != "capacity_verified":
            return
        name, capacity = event.details["resource"], event.details["capacity"]
        previous = self.cached_capacities[name]
        self.cached_capacities[name] = capacity
        try:
            self.store.save(self.preferences())
        except ClashAgentError as exc:
            self.cached_capacities[name] = previous
            self.capacity_error = f"容量保存失败：{exc}"
            self.capacity_control.stop()
            self.detail_label.setText(self.capacity_error)
            self.message.emit(self.capacity_error)
            return
        self._sync_targets()
        self.detail_label.setText(event.message+"，已保存")
        self.message.emit(event.message+"，已保存")

    @Slot(object)
    def _capacity_succeeded(self, values):
        if not self.capacity_error:
            self.detail_label.setText("最大容量已读取并保存；主循环只使用已保存的容量。")
            self.message.emit(self.detail_label.text())

    @Slot(str)
    def _capacity_failed(self, message):
        self.capacity_error = self.capacity_error or message
        self.detail_label.setText(self.capacity_error)
        self.message.emit(self.capacity_error)

    @Slot()
    def _capacity_finished(self):
        self.capacity_task.wait()
        self.capacity_task.deleteLater()
        self.capacity_task = self.capacity_control = None
        self.phase = "failed" if self.capacity_error else "idle"
        self.state_label.setText("容量读取失败" if self.capacity_error else "容量已保存")
        self._update_controls()
        self.active_changed.emit(False)

    @Slot(object)
    def on_event(self, event: FlowEvent):
        if self.task is None or event.run_id != self.task.run_id:
            return
        if (self.phase not in ("pausing", "stopping") or event.kind == "terminal"
                or (self.phase == "pausing" and event.phase == "paused")):
            self.phase = event.phase
        if event.kind not in ("observation", "resources"):
            self.detail_label.setText(event.message)
            self.message.emit(event.message)
        if event.kind == "state":
            self.state_label.setText(event.message)
        self.cycle_label.setText(f"已完成 {event.cycles_completed} 轮")
        self.deployment_label.setText(f"发送 {event.deployments} 次 · 确认 {event.verified_batches} 批")
        if event.frame is not None:
            self.canvas.set_frame(event.frame)
            backend = {"mumu_native": "MuMu 原生", "adb_raw": "ADB Raw", "adb_png": "ADB PNG"}.get(event.frame.backend, event.frame.backend)
            self.frame_label.setText(f"{backend} · {event.frame.width} × {event.frame.height}")
        if event.recognition is not None:
            self.canvas.show_regions(tuple(hit.region for hit in event.recognition.hits) if event.recognition.state else ())
        for reading in event.resources:
            bar = self.bars[reading.resource]
            bar.setValue(min(100, round(reading.percent)))
            bar.setFormat(f"{reading.percent:.1f}% / 目标 {reading.target_percent}%")
            bar.setToolTip(f"最近读取：{reading.value:,} / {reading.capacity:,}")
        self._update_controls()

    @Slot(object)
    def on_result(self, result: FlowResult):
        self.result = result
        self.phase = result.phase
        self.detail_label.setText(result.message)
        self.cycle_label.setText(f"已完成 {result.cycles_completed} 轮")
        self._update_controls()

    @Slot()
    def _finished(self):
        self.task.wait()
        self.task.deleteLater()
        self.task = None
        self.control = None
        self._update_controls()
        self.active_changed.emit(False)

    def _update_controls(self):
        active = self.busy
        self.phase_label.setText(PHASES.get(self.phase, self.phase))
        texts = {"paused": "继续", "pausing": "正在暂停…", "stopping": "正在停止…",
                 "starting": "暂停", "running": "暂停"}
        self.start_button.setText(texts.get(self.phase, "开始刷资源") if active else "开始刷资源")
        self.start_button.setEnabled((not active and self.settings is not None)
                                     or (active and self.phase in ("starting", "running", "paused")))
        self.stop_button.setEnabled(active and self.phase not in ("stopping", "completed", "failed", "stopped"))
        self.percent.setEnabled(not active and not self.builder)
        self.read_capacity_button.setEnabled(not active and self.settings is not None)
        for name in self.targets:
            self.targets[name].setEnabled(not active and not self.builder)
            self.bars[name].setEnabled(self.targets[name].isChecked() and self.cached_capacities[name] > 0)
