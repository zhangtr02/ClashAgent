import json
from pathlib import Path

from core.errors import ClashAgentError
from core.flow.deployment import DeploymentPlan, UnitSpec
from core.flow.models import Anchor
from core.vision.models import Region
from core.vision.template_cache import TemplateCache
from infrastructure.flow_resources import _number
from infrastructure.settings import PROJECT_ROOT

DEFAULT_DEPLOYMENT = PROJECT_ROOT / "resources/flows/deployment.json"


def load_deployment(path: Path = DEFAULT_DEPLOYMENT):
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if data["version"] != 1 or data["reference_size"] != [1280, 720]:
            raise ValueError("不支持的兵表版本或基准尺寸")
        cache = TemplateCache()

        def png(relative):
            target = (path.parent / relative).resolve()
            if not target.is_relative_to(path.parent.resolve()):
                raise ValueError("兵表模板路径越界")
            raw = target.read_bytes()
            cache.prepare(raw)
            return raw

        def anchor(name, spec):
            raw = png(spec["template"])
            roi = Region(*spec["roi"])
            roi.validate(1280, 720)
            h, w = cache.prepare(raw).gray.shape
            if w > roi.width or h > roi.height:
                raise ValueError("兵种图标大于搜索区域")
            return Anchor(name, raw, roi, _number(spec, "threshold", .9, .7, 1),
                          _number(spec, "max_pixel_error", 30, 0, 100))

        if not isinstance(data["units"], list) or not 1 <= len(data["units"]) <= 16:
            raise ValueError("兵种数量不合法")
        units = []
        for item in data["units"]:
            name = item["id"]
            if not isinstance(name, str) or not name or item["kind"] != "troop":
                raise ValueError("兵种 ID 或类别不合法")
            if not isinstance(item["label"], str) or not item["label"].strip():
                raise ValueError("兵种显示名称不合法")
            offset = item.get("counter_offset")
            if item["kind"] == "troop":
                if (not isinstance(offset, list) or len(offset) != 4 or any(type(v) is not int for v in offset)
                        or not 1 <= offset[2] <= 150 or not 1 <= offset[3] <= 50
                        or any(abs(v) > 150 for v in offset[:2])):
                    raise ValueError("兵数区域不合法")
            units.append(UnitSpec(name, item["label"], item["kind"], anchor(name, item["portrait"]),
                                  tuple(offset) if offset else None))
        if len({u.name for u in units}) != len(units) or not any(u.kind == "troop" for u in units):
            raise ValueError("兵表存在重复 ID 或没有普通兵种")
        return DeploymentPlan(tuple(units), png(data["counter_prefix"]), png(data["counter_zero"]),
                              blocked_png=png(data["deployment_blocked"]))
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ClashAgentError(f"兵表资源校验失败：{exc}") from exc
