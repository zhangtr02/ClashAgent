import json
import math
import re
from pathlib import Path

from core.errors import ClashAgentError
from core.flow.models import Anchor, FlowDefinition, FlowNode
from core.vision.models import Region
from core.vision.template_cache import TemplateCache
from infrastructure.settings import PROJECT_ROOT

DEFAULT_FLOW = PROJECT_ROOT / "resources" / "flows" / "regular_battle.json"


def _number(data, name, default, minimum, maximum):
    value = data.get(name, default)
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} 应介于 {minimum} 和 {maximum} 之间")
    return value


def load_flow(path: Path = DEFAULT_FLOW) -> FlowDefinition:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if data.get("version") != 1:
            raise ValueError("不支持的流程版本")
        width, height = data["reference_size"]
        if type(width) is not int or type(height) is not int or not 1 <= width <= 4096 or not 1 <= height <= 4096:
            raise ValueError("基准截图尺寸不合法")
        if not isinstance(data["nodes"], list) or not 1 <= len(data["nodes"]) <= 32:
            raise ValueError("流程节点数量不合法")
        cache = TemplateCache()
        nodes = []
        for item in data["nodes"]:
            name, label = item["id"], item["label"]
            if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", name):
                raise ValueError("节点 ID 不合法")
            if not isinstance(label, str) or not label.strip():
                raise ValueError("节点缺少显示名称")
            action = item["action"]
            if not isinstance(action, dict) or action["type"] not in ("click", "wait", "complete", "pause"):
                raise ValueError(f"{name} 包含不支持的动作")
            algorithm = item.get("algorithm", "template")
            if algorithm not in ("template", "clouds"):
                raise ValueError(f"{name} 包含不支持的识别算法")
            anchors = []
            if not isinstance(item.get("anchors", []), list) or len(item.get("anchors", [])) > 8:
                raise ValueError("节点识别特征数量不合法")
            for spec in item.get("anchors", []):
                template_path = (path.parent / spec["template"]).resolve()
                if not template_path.is_relative_to(path.parent.resolve()):
                    raise ValueError("模板路径不能超出当前资源目录")
                png = template_path.read_bytes()
                template = cache.prepare(png).gray
                roi = Region(*spec["roi"])
                roi.validate(width, height)
                if template.shape[1] > roi.width or template.shape[0] > roi.height:
                    raise ValueError("模板不能大于搜索区域")
                threshold = _number(spec, "threshold", .9, 0, 1)
                if not isinstance(spec["id"], str) or not spec["id"]:
                    raise ValueError("识别特征缺少 ID")
                if type(spec.get("text_mask", False)) is not bool:
                    raise ValueError("text_mask 必须为布尔值")
                mask_png = None
                if spec.get("mask"):
                    mask_path = (path.parent / spec["mask"]).resolve()
                    if not mask_path.is_relative_to(path.parent.resolve()):
                        raise ValueError("模板掩码路径越界")
                    mask_png = mask_path.read_bytes()
                    if cache.prepare(mask_png).gray.shape != template.shape or spec.get("text_mask"):
                        raise ValueError("掩码尺寸必须与模板一致，且不能与自动文字掩码同时使用")
                anchors.append(Anchor(spec["id"], png, roi, threshold,
                                      _number(spec, "max_pixel_error", 25, 0, 255), spec.get("text_mask", False), mask_png))
            if len({anchor.name for anchor in anchors}) != len(anchors):
                raise ValueError("节点存在重复的识别特征 ID")
            if algorithm == "template" and not anchors:
                raise ValueError(f"{name} 缺少识别特征")
            if algorithm == "clouds" and (anchors or action["type"] != "wait"):
                raise ValueError("云层识别只允许等待，不允许输入")
            target = action.get("target", "")
            if action["type"] == "click" and target not in {anchor.name for anchor in anchors}:
                raise ValueError(f"{name} 点击目标不存在")
            next_nodes = item.get("next", [])
            if not isinstance(next_nodes, list) or not all(isinstance(value, str) for value in next_nodes):
                raise ValueError("next 必须为节点 ID 列表")
            if action["type"] in ("click", "wait") and not next_nodes:
                raise ValueError(f"{name} 缺少后续验证节点")
            attempts = _number(item, "max_attempts", 2, 1, 5)
            if type(attempts) is not int:
                raise ValueError("max_attempts 必须为整数")
            nodes.append(FlowNode(name, label, tuple(anchors), action["type"], target, tuple(next_nodes),
                                  _number(item, "timeout", 10, .1, 120),
                                  _number(item, "retry_interval", 3, .1, 30), attempts,
                                  algorithm, item.get("message", "")))
        names = {node.name for node in nodes}
        if len(names) != len(nodes):
            raise ValueError("流程节点 ID 重复")
        start = data["start"]
        if not isinstance(start, list) or not start or not all(isinstance(name, str) and name in names for name in start):
            raise ValueError("起始候选节点不存在")
        if any(name not in names for node in nodes for name in node.next):
            raise ValueError("流程引用了不存在的后续节点")
        if not any(node.action == "complete" for node in nodes):
            raise ValueError("流程缺少完成节点")
        stable = _number(data, "stable_frames", 2, 1, 10)
        limit = _number(data, "max_actions", 8, 1, 50)
        if type(stable) is not int or type(limit) is not int:
            raise ValueError("稳定帧数与动作上限必须为整数")
        return FlowDefinition(data["id"], data["label"], width, height, tuple(nodes), tuple(start), stable,
                              _number(data, "poll_interval", .2, .02, 5),
                              _number(data, "unknown_timeout", 8, .1, 120), limit)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, StopIteration) as exc:
        raise ClashAgentError(f"流程资源校验失败：{exc}") from exc
