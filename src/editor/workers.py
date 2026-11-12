from collections.abc import Callable
from datetime import datetime
from uuid import uuid4
from dataclasses import asdict

from PySide6.QtCore import QThread, Signal

from core.errors import ClashAgentError
from core.service import DeviceService
from core.discovery import DiscoverySnapshot
from infrastructure.adb import AdbClient
from infrastructure.settings import Settings
from infrastructure.mumu_discovery import MuMuDiscovery
from core.flow.control import RunControl, FlowStopped
from core.flow.recognizer import StateRecognizer
from core.flow.resource_capacity import read_capacities
from core.flow.models import FlowEvent, FlowResult
from infrastructure.capture_backends import build_capture_manager
from infrastructure.builder_resources import load_builder_battle
from infrastructure.flow_device import AndroidFlowDevice
from infrastructure.flow_resources import load_flow
from infrastructure.deployment_resources import load_deployment
from core.flow.cycle_runner import CycleRunner
from core.flow.builder_runner import BuilderCycleRunner
from core.resources import FarmOptions
from infrastructure.resource_reader import load_resource_reader
from infrastructure.run_recorder import RunRecorder


class DeviceTask(QThread):
    progress = Signal(str)
    succeeded = Signal(str, object)
    failed = Signal(str, str)

    def __init__(self, operation: str, settings: Settings, parent=None):
        super().__init__(parent)
        self.operation = operation
        self.settings = settings

    def run(self) -> None:
        try:
            client = AdbClient(self.settings.adb_path, self.settings.endpoint)
            service = DeviceService(client, self.settings.endpoint, self.progress.emit)
            operations: dict[str, Callable[[], object]] = {
                "connect": service.connect,
                "refresh": service.inspect,
                "disconnect": service.disconnect,
                "launch": lambda: service.launch_game(self.settings.package),
            }
            self.succeeded.emit(self.operation, operations[self.operation]())
        except ClashAgentError as exc:
            self.failed.emit(self.operation, str(exc))
        except Exception as exc:
            self.failed.emit(self.operation, f"未预期的错误：{type(exc).__name__}: {exc}")


class DiscoveryTask(QThread):
    succeeded = Signal(object)

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.settings = settings

    def run(self) -> None:
        states, instances, adb_error, discovery_error = {}, (), "", ""
        try:
            states = AdbClient(self.settings.adb_path, self.settings.endpoint).device_states()
        except Exception as exc:
            adb_error = str(exc)
        if self.settings.connection_mode == "auto":
            try:
                online = tuple(serial for serial, state in states.items() if state == "device")
                instances = MuMuDiscovery().discover(online)
            except Exception as exc:
                discovery_error = str(exc)
        self.succeeded.emit(DiscoverySnapshot(instances, states, datetime.now().astimezone(), discovery_error, adb_error))


class FlowTask(QThread):
    progress = Signal(object)
    completed = Signal(object)

    def __init__(self, settings: Settings, control: RunControl, parent=None, *, options=None, builder=False):
        super().__init__(parent)
        self.settings, self.control = settings, control
        self.run_id = uuid4().hex
        self.options = options or FarmOptions()
        self.builder = builder

    def run(self) -> None:
        manager = None
        device = None
        try:
            # Validate every node/template before creating an input-capable device.
            self.options.validate()
            if self.builder:
                definition, vision = load_builder_battle()
            else:
                definition = load_flow()
                plan = load_deployment()
                resource_reader = load_resource_reader()
            recorder = RunRecorder(self.run_id)
            strategy = "builder_witch_two_stage" if self.builder else "normalized_camera_verified_points"
            recorder.metadata({"flow": definition.name, "options": asdict(self.options), "strategy": strategy})

            def emit(event):
                recorder.record(event)
                self.progress.emit(event)

            manager = build_capture_manager(self.settings, AdbClient)
            device = AndroidFlowDevice(self.settings, manager)
            if self.builder:
                runner = BuilderCycleRunner(definition, vision, device, self.control, emit, self.options, self.run_id)
            else:
                runner = CycleRunner(definition, plan, device, self.control, emit, self.options, self.run_id,
                                     resource_reader=resource_reader)
            result = runner.run()
        except Exception as exc:
            message = f"任务失败：{exc}"
            self.progress.emit(FlowEvent("terminal", "failed", message=message, run_id=self.run_id))
            result = FlowResult("failed", "", message, self.run_id)
        finally:
            if device is not None:
                try:
                    device.close()
                except Exception as exc:
                    self.progress.emit(FlowEvent("warning", "stopped", message=f"触控会话清理失败：{exc}", run_id=self.run_id))
            if manager is not None:
                try:
                    manager.close()
                except Exception as exc:
                    message = f"截图会话清理失败：{exc}"
                    self.progress.emit(FlowEvent("terminal", "failed", message=message, run_id=self.run_id))
                    result = FlowResult("failed", result.node, message, self.run_id, result.cycles_completed)
        self.completed.emit(result)


class CapacityTask(QThread):
    progress = Signal(object)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, settings, control, parent=None, *, builder=False):
        super().__init__(parent)
        self.settings, self.control = settings, control
        self.builder = builder

    def run(self):
        manager = None
        try:
            manager = build_capture_manager(self.settings, AdbClient)
            device = AndroidFlowDevice(self.settings, manager)
            device.ensure_foreground()
            result = read_capacities(load_resource_reader(builder=self.builder), device, self.control,
                                     StateRecognizer(load_flow()), self.progress.emit, builder=self.builder)
            self.succeeded.emit(result)
        except FlowStopped:
            self.failed.emit("容量读取已取消；已确认的容量保留在配置中。")
        except Exception as exc:
            self.failed.emit(f"容量读取失败：{exc}")
        finally:
            if manager is not None:
                manager.close()
