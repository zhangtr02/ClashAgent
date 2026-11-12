import json
from pathlib import Path

import cv2

from core.capture import decode_png
from core.errors import ClashAgentError
from core.resources import RESOURCE_LABELS, ResourceReader, normalize_digit, white_mask
from core.vision.models import Region
from infrastructure.settings import PROJECT_ROOT


def load_resource_reader(path: Path | None = None, *, builder=False) -> ResourceReader:
    path = path or PROJECT_ROOT / "resources/hud/atlas.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if data["version"] != 1 or data["reference_size"] != [1280, 720]:
            raise ValueError("不支持的资源数字版本")
        if set(data["digits"]) != set("0123456789"):
            raise ValueError("资源数字模板必须包含 0–9")
        digits = {}
        for digit, names in data["digits"].items():
            if not isinstance(names, list) or not 1 <= len(names) <= 10:
                raise ValueError("数字样本数量不合法")
            variants = []
            for name in names:
                target = (path.parent/name).resolve()
                if not target.is_relative_to(path.parent.resolve()):
                    raise ValueError("数字模板路径越界")
                variants.append(normalize_digit(white_mask(decode_png(target.read_bytes()))))
            digits[digit] = tuple(variants)
        regions = {name: Region(*data["regions"][name]) for name in RESOURCE_LABELS}
        for region in regions.values():
            region.validate(1280, 720)
        labels = []
        names = ('capacity-label-builder.png',) if builder else ('capacity-label.png', 'capacity-label-gold.png')
        for name in names:
            label = decode_png((path.parent/'templates'/name).read_bytes())
            label = cv2.cvtColor(label, cv2.COLOR_BGR2GRAY)
            if not (15 <= label.shape[0] <= 32 and 70 <= label.shape[1] <= 110) or int((label > 127).sum()) < 100:
                raise ValueError("最大储量识别素材无效")
            labels.append(label)
        return ResourceReader(digits, regions, tuple(labels))
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ClashAgentError(f"资源数字模板加载失败：{exc}") from exc
