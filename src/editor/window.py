from datetime import datetime

from PySide6.QtCore import Qt, Slot, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QMainWindow, QPlainTextEdit, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from core.errors import ClashAgentError
from core.models import DeviceInfo, LaunchResult
from core.discovery import DiscoverySnapshot, MuMuInstance, select_instance
from editor.flow_panel import FlowPanel
from editor.workers import DeviceTask, DiscoveryTask
from infrastructure.settings import PROJECT_ROOT, Settings, SettingsStore
from infrastructure.farm_settings import FarmSettingsStore


class MainWindow(QMainWindow):
    def __init__(self, store: SettingsStore | None = None, enable_monitor: bool = True,
                 farm_store: FarmSettingsStore | None = None):
        super().__init__()
        self.store = store or SettingsStore()
        self.farm_store = farm_store
        self.task: DeviceTask | None = None
        self.connected_settings: Settings | None = None
        self.monitor_task: DiscoveryTask | None = None
        self.snapshot: DiscoverySnapshot | None = None
        self._selected_key: str | None = None
        self._allow_single_replacement = True
        self._connection_requested = False
        self._task_automatic = False
        self._last_monitor_error = ""
        self._last_device_error = ""
        self._closing = False
        self._pending_operation = None
        self.setWindowTitle("ClashAgent · Editor")
        self.resize(1280, 860)
        self.setMinimumSize(1040, 760)
        self._build_ui()
        try:
            settings = self.store.load()
        except ClashAgentError as exc:
            settings = Settings()
            self._log(str(exc), "错误")
        self.adb_path.setText(settings.adb_path)
        self.endpoint.setText(settings.endpoint)
        self.package.setText(settings.package)
        self.mode.blockSignals(True)
        self.mode.setCurrentIndex(0 if settings.connection_mode == "auto" else 1)
        self.mode.blockSignals(False)
        self._log("Editor 已就绪。请先手动打开 MuMu，进入 Android 桌面后连接。")
        if self.flow_panel.load_error:
            self._log(self.flow_panel.load_error, "错误")
        self._update_controls()
        self.monitor_timer = QTimer(self)
        self.monitor_timer.setInterval(3000)
        self.monitor_timer.timeout.connect(self.request_discovery)
        if enable_monitor:
            self.monitor_timer.start()
            QTimer.singleShot(0, self.request_discovery)

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        shell = QHBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(170)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(15, 25, 15, 20)
        brand = QLabel("ClashAgent")
        brand.setObjectName("brand")
        sidebar_layout.addWidget(brand)
        subtitle = QLabel("固定兵表 · 持续刷资源")
        subtitle.setObjectName("hint")
        sidebar_layout.addWidget(subtitle)
        self.navigation = QListWidget()
        self.navigation.setObjectName("navigation")
        self.navigation.addItems(["村庄刷资源", "夜世界刷资源", "设备连接"])
        sidebar_layout.addWidget(self.navigation, 1)
        version = QLabel("Python / MuMu\nv0.8.0")
        version.setObjectName("hint")
        sidebar_layout.addWidget(version)
        shell.addWidget(sidebar)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(22, 20, 22, 16)
        layout.setSpacing(12)
        heading = QHBoxLayout()
        self.page_title = QLabel("村庄刷资源")
        self.page_title.setObjectName("title")
        heading.addWidget(self.page_title)
        heading.addStretch()
        self.connection_badge = QLabel("未连接")
        self.connection_badge.setObjectName("status")
        heading.addWidget(self.connection_badge)
        layout.addLayout(heading)
        self.pages = QStackedWidget()
        self.flow_panel = FlowPanel(self._prepare_flow, self.farm_store)
        self.builder_panel = FlowPanel(self._prepare_flow, FarmSettingsStore(PROJECT_ROOT / "data/builder_farm.json"),
                                       builder=True)
        self.flow_panels = (self.flow_panel, self.builder_panel)
        for panel in self.flow_panels:
            panel.message.connect(self._log)
            panel.active_changed.connect(self._flow_activity_changed)
            self.pages.addWidget(panel)
        device_page = QWidget()
        device_page_layout = QVBoxLayout(device_page)
        device_page_layout.setContentsMargins(0, 0, 0, 0)
        hint = QLabel("请先手动打开 MuMu 并启用 ADB。程序自动发现运行中的实例，不启动模拟器。")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        device_page_layout.addWidget(hint)
        columns = QHBoxLayout()
        columns.setSpacing(18)
        settings_group = QGroupBox("连接配置")
        settings_layout = QFormLayout(settings_group)
        settings_layout.setSpacing(13)
        self.adb_path = QLineEdit()
        self.adb_path.setPlaceholderText("留空自动使用 MuMu 自带或本机 ADB")
        self.browse_button = QPushButton("浏览…")
        self.browse_button.clicked.connect(self._browse_adb)
        adb_row = QHBoxLayout()
        adb_row.addWidget(self.adb_path)
        adb_row.addWidget(self.browse_button)
        settings_layout.addRow("ADB 程序", adb_row)
        self.mode = QComboBox()
        self.mode.addItem("自动发现并监测（推荐）", "auto")
        self.mode.addItem("手动指定地址", "manual")
        self.mode.currentIndexChanged.connect(self._mode_changed)
        settings_layout.addRow("连接方式", self.mode)
        self.instances = QComboBox()
        self.instances.setMinimumContentsLength(20)
        self.instances.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.instances.addItem("等待发现运行中的 MuMu…", None)
        self.instances.currentIndexChanged.connect(self._instance_changed)
        self.discover_button = QPushButton("立即发现")
        self.discover_button.clicked.connect(self.request_discovery)
        discovery_row = QHBoxLayout()
        discovery_row.addWidget(self.instances, 1)
        discovery_row.addWidget(self.discover_button)
        settings_layout.addRow("运行实例", discovery_row)
        self.endpoint = QLineEdit()
        settings_layout.addRow("实例地址", self.endpoint)
        endpoint_hint = QLabel("自动模式显示实测 ADB 地址；多开时请选择目标实例。手动模式可自行填写。")
        endpoint_hint.setObjectName("hint")
        endpoint_hint.setWordWrap(True)
        settings_layout.addRow("", endpoint_hint)
        self.package = QLineEdit()
        settings_layout.addRow("游戏包名", self.package)
        self.save_button = QPushButton("保存配置")
        self.save_button.clicked.connect(self._save_settings)
        settings_layout.addRow("", self.save_button)
        columns.addWidget(settings_group, 3)

        device_group = QGroupBox("当前设备")
        device_layout = QFormLayout(device_group)
        device_layout.setSpacing(16)
        self.status_label = QLabel("未连接")
        self.status_label.setObjectName("status")
        device_layout.addRow("连接状态", self.status_label)
        self.device_labels: dict[str, QLabel] = {}
        for key, caption in (("serial", "设备地址"), ("model", "设备型号"),
                             ("android", "Android"), ("resolution", "屏幕尺寸"), ("game", "游戏状态")):
            value = QLabel("—")
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.device_labels[key] = value
            device_layout.addRow(caption, value)
        columns.addWidget(device_group, 2)
        device_page_layout.addLayout(columns)

        actions = QHBoxLayout()
        self.connect_button = QPushButton("连接 MuMu")
        self.connect_button.setObjectName("primary")
        self.refresh_button = QPushButton("刷新设备")
        self.disconnect_button = QPushButton("断开连接")
        self.launch_button = QPushButton("启动部落冲突")
        self.launch_button.setObjectName("primary")
        for button, operation in ((self.connect_button, "connect"), (self.refresh_button, "refresh"),
                                  (self.disconnect_button, "disconnect"), (self.launch_button, "launch")):
            button.clicked.connect(lambda checked=False, op=operation: self._start_task(op))
            actions.addWidget(button)
        actions.addStretch()
        device_page_layout.addLayout(actions)
        self.monitor_label = QLabel("尚未监测")
        self.monitor_label.setObjectName("hint")
        self.monitor_label.setWordWrap(True)
        device_page_layout.addWidget(self.monitor_label)
        device_page_layout.addStretch()

        self.pages.addWidget(device_page)
        self.navigation.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.navigation.currentRowChanged.connect(
            lambda index: self.page_title.setText(self.navigation.item(index).text()) if index >= 0 else None)
        self.navigation.setCurrentRow(0)
        layout.addWidget(self.pages, 1)
        logs = QGroupBox("运行日志")
        log_layout = QVBoxLayout(logs)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(700)
        self.log_view.setMinimumHeight(70)
        self.log_view.setMaximumHeight(135)
        log_layout.addWidget(self.log_view)
        layout.addWidget(logs)
        shell.addWidget(content, 1)
        self.statusBar().showMessage("下兵前会自动统一视角；运行时请勿手动拖动地图。")

    def _read_settings(self) -> Settings:
        settings = Settings(self.adb_path.text().strip().strip('"'), self.endpoint.text().strip(),
                            self.package.text().strip(), self.mode.currentData())
        settings.validate()
        return settings

    @Slot()
    def _browse_adb(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择 ADB 程序", "", "可执行程序 (*.exe);;所有文件 (*)")
        if path:
            self.adb_path.setText(path)

    @Slot()
    def _save_settings(self) -> None:
        try:
            self.store.save(self._read_settings())
            self._log(f"配置已保存：{self.store.path}")
        except ClashAgentError as exc:
            self._log(str(exc), "错误")

    def _start_task(self, operation: str, automatic: bool = False) -> None:
        if self.task is not None:
            return
        active_panel = next((panel for panel in self.flow_panels if panel.busy), None)
        if active_panel is not None:
            if operation == "disconnect":
                self._connection_requested = False
                self._pending_operation = operation
                active_panel.stop()
            return
        if operation == "disconnect":
            self._connection_requested = False
            if self.connected_settings is None:
                self.status_label.setText("未连接 · 已停止自动重连")
                self._update_controls()
                return
        if operation == "connect":
            self._connection_requested = True
            if self.mode.currentData() == "auto":
                instance = self.instances.currentData()
                if not isinstance(instance, MuMuInstance) or not instance.endpoints:
                    self.status_label.setText("等待发现 MuMu 实例…")
                    self._update_controls()
                    self.request_discovery()
                    return
                self._selected_key = instance.key
                self.endpoint.setText(instance.endpoint(self.endpoint.text()))
        try:
            settings = self._read_settings()
            self.store.save(settings)
            if operation != "connect" and settings != self.connected_settings:
                raise ClashAgentError("连接配置已变更，请先重新连接 MuMu。")
        except ClashAgentError as exc:
            self._log(str(exc), "错误")
            if operation == "connect":
                self._connection_requested = False
                self._update_controls()
            return
        if operation == "connect":
            self.connected_settings = None
            self._clear_device()
        self.status_label.setText("正在操作…")
        self._task_automatic = automatic
        self.statusBar().showMessage("任务执行中，请稍候…")
        self.task = DeviceTask(operation, settings, self)
        self.task.progress.connect(self._task_progress)
        self.task.succeeded.connect(self._on_success)
        self.task.failed.connect(self._on_failure)
        self.task.finished.connect(self._on_finished)
        self._update_controls()
        self.task.start()

    @Slot(str, object)
    def _on_success(self, operation: str, result: object) -> None:
        assert self.task is not None
        if isinstance(result, DeviceInfo):
            self.connected_settings = self.task.settings
            for key, value in (("serial", result.serial), ("model", result.model),
                               ("android", result.android_version), ("resolution", result.resolution)):
                self.device_labels[key].setText(value)
            self.device_labels["game"].setText("尚未检查" if operation == "connect" else "需启动后确认")
            self.endpoint.setText(result.serial)
            self._last_device_error = ""
            if operation == "connect" and self._task_automatic:
                self._log(f"连接已恢复：{result.serial}")
        elif isinstance(result, LaunchResult):
            self.device_labels["game"].setText("已在前台 · 登录/加载状态待确认")
        elif operation == "disconnect":
            self.connected_settings = None
            self._clear_device()
        self.status_label.setText("已连接" if self.connected_settings else "未连接")
        self.statusBar().showMessage("操作完成")

    @Slot(str, str)
    def _on_failure(self, operation: str, message: str) -> None:
        if not self._task_automatic or message != self._last_device_error:
            self._log(message, "错误")
        self._last_device_error = message
        # Invalidate stale connection information after any failed device task.
        self.connected_settings = None
        self._clear_device()
        self.status_label.setText("连接异常 · 等待自动重连" if self._connection_requested else "操作失败 · 请重新连接")
        self.statusBar().showMessage("操作失败，详情见运行日志")

    @Slot()
    def _on_finished(self) -> None:
        if self.task is not None:
            self.task.wait()
            self.task.deleteLater()
            self.task = None
        self._update_controls()

    def _clear_device(self) -> None:
        for label in self.device_labels.values():
            label.setText("—")

    def _update_controls(self) -> None:
        flow_active = any(panel.busy for panel in self.flow_panels)
        idle = self.task is None and not flow_active and not self._closing
        connected = self.connected_settings is not None
        self.connect_button.setEnabled(idle)
        for widget in (self.refresh_button, self.launch_button):
            widget.setEnabled(idle and connected)
        self.disconnect_button.setEnabled(self.task is None and not self._closing and (connected or self._connection_requested))
        for widget in (self.adb_path, self.endpoint, self.package, self.browse_button, self.save_button):
            widget.setEnabled(idle and not connected)
        self.mode.setEnabled(idle and not connected)
        self.endpoint.setReadOnly(self.mode.currentData() == "auto")
        self.instances.setEnabled(idle and self.mode.currentData() == "auto")
        self.discover_button.setEnabled(idle and self.monitor_task is None)
        self.connection_badge.setText(self.status_label.text())
        for panel in self.flow_panels:
            available = self.task is None and (panel.busy or not flow_active)
            panel.set_session(self.connected_settings if available else None, self._selected_key)

    def _prepare_flow(self) -> Settings:
        if self._closing or self.task is not None or any(panel.busy for panel in self.flow_panels):
            raise ClashAgentError("请等待设备操作结束后开始。")
        if self.connected_settings is None:
            raise ClashAgentError("请先连接 MuMu。")
        if self._read_settings() != self.connected_settings:
            raise ClashAgentError("连接配置已改变，请重新连接。")
        return self.connected_settings

    @Slot(bool)
    def _flow_activity_changed(self, active: bool) -> None:
        self._update_controls()
        if not active and not self._closing:
            operation, self._pending_operation = self._pending_operation, None
            if operation:
                self._start_task(operation)
            else:
                self.request_discovery()

    @Slot(str)
    def _task_progress(self, message: str) -> None:
        if not self._task_automatic:
            self._log(message)

    @Slot()
    def _mode_changed(self) -> None:
        self._connection_requested = False
        self._selected_key = None
        self._allow_single_replacement = True
        self.snapshot = None
        self._update_controls()
        if hasattr(self, "monitor_timer"):
            self.request_discovery()

    @Slot()
    def _instance_changed(self) -> None:
        instance = self.instances.currentData()
        if not isinstance(instance, MuMuInstance):
            return
        if instance.key != self._selected_key:
            self._selected_key = instance.key
            self._allow_single_replacement = bool(self.snapshot and len(self.snapshot.instances) == 1)
            self._connection_requested = False
            self.connected_settings = None
            self._clear_device()
            self.status_label.setText("已切换目标 · 请连接")
        self.endpoint.setText(instance.endpoint(self.endpoint.text()))
        self._update_controls()

    @Slot()
    def request_discovery(self) -> None:
        if self._closing or self.monitor_task is not None or self.task is not None:
            return
        try:
            settings = self._read_settings()
        except ClashAgentError:
            return
        self.monitor_task = DiscoveryTask(settings, self)
        self.monitor_task.succeeded.connect(self._on_snapshot)
        self.monitor_task.finished.connect(self._monitor_finished)
        self.discover_button.setEnabled(False)
        self.monitor_task.start()

    @Slot(object)
    def _on_snapshot(self, snapshot: DiscoverySnapshot) -> None:
        if self._closing or self.monitor_task is None:
            return
        if (self.monitor_task.settings.adb_path != self.adb_path.text().strip().strip('"')
                or self.monitor_task.settings.connection_mode != self.mode.currentData()):
            return
        self.snapshot = snapshot
        errors = "；".join(filter(None, (snapshot.discovery_error, snapshot.adb_error)))
        if errors != self._last_monitor_error:
            if errors:
                self._log(f"监测异常：{errors}", "错误")
            elif self._last_monitor_error:
                self._log("实例监测已恢复。")
            self._last_monitor_error = errors
        self.monitor_label.setText(f"{snapshot.observed_at:%H:%M:%S} · 发现 {len(snapshot.instances)} 个 MuMu 实例 · "
                                   + (errors or "ADB 服务正常 · 每 3 秒更新"))
        if self.task is not None:
            return
        automatic = self.mode.currentData() == "auto"
        previous_key = self._selected_key
        instance = None
        if automatic and not snapshot.discovery_error:
            if len(snapshot.instances) > 1:
                self._allow_single_replacement = False
            instance = select_instance(snapshot.instances, self._selected_key, self._allow_single_replacement)
            self.instances.blockSignals(True)
            self.instances.clear()
            for item in snapshot.instances:
                address = item.endpoint(self.endpoint.text()) or "ADB 尚未就绪"
                self.instances.addItem(f"{item.name} · PID {item.pid} · {address}", item)
            if instance:
                self._selected_key = instance.key
                self.instances.setCurrentIndex(next(i for i, item in enumerate(snapshot.instances) if item.key == instance.key))
                self.endpoint.setText(instance.endpoint(self.endpoint.text()))
            else:
                self.instances.insertItem(0, "请选择目标实例" if snapshot.instances else "未发现 MuMu，请先打开模拟器", None)
                self.instances.setCurrentIndex(0)
            self.instances.blockSignals(False)

        if self.connected_settings:
            state = snapshot.device_states.get(self.connected_settings.endpoint)
            target_gone = automatic and not snapshot.discovery_error and (
                instance is None or self.connected_settings.endpoint not in instance.endpoints
                or (previous_key is not None and previous_key != instance.key))
            if snapshot.adb_error or state != "device" or target_gone:
                self.connected_settings = None
                self._clear_device()
                self.status_label.setText("设备离线 · 等待恢复")
                self._log("连接已失效，已终止当前任务并禁用设备操作。")
            else:
                self.status_label.setText("已连接 · 实时监测")
        self._update_controls()
        if (self._connection_requested and self.connected_settings is None
                and not snapshot.adb_error and not self._closing
                and (not automatic or (instance is not None and bool(instance.endpoints)))):
            self._start_task("connect", automatic=True)

    @Slot()
    def _monitor_finished(self) -> None:
        if self.monitor_task is not None:
            self.monitor_task.wait()
            self.monitor_task.deleteLater()
            self.monitor_task = None
        self._update_controls()

    @Slot(str)
    def _log(self, message: str, level: str = "信息") -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{stamp}] [{level}] {message}")

    def closeEvent(self, event: QCloseEvent) -> None:
        self._closing = True
        self.monitor_timer.stop()
        self._pending_operation = None
        for panel in self.flow_panels:
            panel.stop()
        if self.task is not None or self.monitor_task is not None or any(panel.busy for panel in self.flow_panels):
            self.statusBar().showMessage("正在停止任务并等待后台操作退出…")
            event.ignore()
            self._update_controls()
            QTimer.singleShot(100, self.close)
        else:
            event.accept()
