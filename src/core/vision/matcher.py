import math

import cv2
import numpy as np

from core.capture import ScreenFrame
from core.errors import ClashAgentError
from core.vision.models import MatchResult, Region, TemplateProfile
from core.vision.template_cache import DEFAULT_TEMPLATE_CACHE, TemplateCache


def match_template(frame: ScreenFrame, profile: TemplateProfile, template_png: bytes,
                   threshold: float | None = None, *, cache: TemplateCache | None = None) -> MatchResult:
    if (frame.width, frame.height) != (profile.frame_width, profile.frame_height):
        raise ClashAgentError("截图尺寸或方向与模板标定不一致，请重新标定；不会自动缩放模板。")
    cutoff = profile.threshold if threshold is None else threshold
    if not math.isfinite(cutoff) or not 0 <= cutoff <= 1:
        raise ClashAgentError("匹配阈值应介于 0 和 1 之间。")
    image = frame.gray
    template = (cache or DEFAULT_TEMPLATE_CACHE).prepare(template_png).gray
    if template.shape != (profile.crop.height, profile.crop.width):
        raise ClashAgentError("模板图片尺寸与标定记录不一致。")
    roi = profile.search_region or Region(0, 0, frame.width, frame.height)
    roi.validate(frame.width, frame.height)
    if roi.width < template.shape[1] or roi.height < template.shape[0]:
        raise ClashAgentError("搜索区域不能小于模板。")
    haystack = image[roi.y:roi.y + roi.height, roi.x:roi.x + roi.width]
    scores = cv2.matchTemplate(haystack, template, cv2.TM_CCOEFF_NORMED)
    scores = np.nan_to_num(scores, nan=-1, posinf=-1, neginf=-1)
    _, score, _, point = cv2.minMaxLoc(scores)
    region = Region(roi.x + point[0], roi.y + point[1], template.shape[1], template.shape[0])
    return MatchResult(profile.template_id, frame.frame_id, region, max(-1.0, min(1.0, score)), cutoff)
