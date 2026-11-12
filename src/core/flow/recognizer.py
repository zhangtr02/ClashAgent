import math

import cv2
import numpy as np

from core.capture import ScreenFrame
from core.errors import ClashAgentError
from core.flow.models import AnchorHit, FlowDefinition, Recognition
from core.vision.matcher import match_template
from core.vision.models import MatchResult, Region, TemplateProfile
from core.vision.template_cache import TemplateCache


class StateRecognizer:
    def __init__(self, definition: FlowDefinition):
        self.definition = definition
        self.cache = TemplateCache()
        self._masks = {}
        self._last_frame = None
        self._last_result = None

    def recognize(self, frame: ScreenFrame) -> Recognition:
        if frame is self._last_frame:
            return self._last_result
        width, height = self.definition.reference_width, self.definition.reference_height
        if abs(frame.width / frame.height - width / height) > 0.01:
            raise ClashAgentError("截图宽高比与流程资源不一致，请使用 16:9 横屏或重新标定流程。")
        pixels = frame.image if (frame.width, frame.height) == (width, height) else cv2.resize(
            frame.image, (width, height), interpolation=cv2.INTER_AREA)
        scaled = ScreenFrame.from_bgr(pixels, frame.source)
        candidates, matched = [], []
        for node in self.definition.nodes:
            hits = []
            if node.algorithm == "clouds":
                # Clouds are a read-only waiting state, never an input target.
                hsv = cv2.cvtColor(pixels, cv2.COLOR_BGR2HSV)
                brightness = scaled.gray
                cloud_fraction = float(np.mean((brightness > 205) & (hsv[:, :, 1] < 40)))
                score = cloud_fraction
                valid = cloud_fraction > 0.9 and float(brightness.mean()) > 225
            else:
                valid, score = True, 1.0
                for anchor in node.anchors:
                    template = self.cache.prepare(anchor.png).gray
                    h, w = template.shape
                    profile = TemplateProfile(anchor.name, anchor.name, width, height,
                                              Region(0, 0, w, h), anchor.roi, anchor.threshold)
                    mask = None
                    if anchor.text_mask or anchor.mask_png:
                        # Compare white glyphs and their immediate outlines;
                        # village scenery behind HUD text changes per opponent.
                        if anchor not in self._masks:
                            self._masks[anchor] = (
                                (self.cache.prepare(anchor.mask_png).gray > 127).astype(np.uint8) if anchor.mask_png else
                                cv2.dilate((template > 220).astype(np.uint8), np.ones((3, 3), np.uint8)))
                        mask = self._masks[anchor]
                        if int(mask.sum()) < 5:
                            raise ClashAgentError(f"文字模板 {anchor.name} 缺少可用前景")
                        roi = anchor.roi
                        search = scaled.gray[roi.y:roi.y+roi.height, roi.x:roi.x+roi.width]
                        scores = cv2.matchTemplate(search, template, cv2.TM_SQDIFF_NORMED, mask=mask)
                        scores = np.nan_to_num(scores, nan=1, posinf=1, neginf=1)
                        minimum, _, location, _ = cv2.minMaxLoc(scores)
                        score_value = max(0, 1 - math.sqrt(max(0, minimum)))
                        result = MatchResult(anchor.name, scaled.frame_id,
                                             Region(roi.x+location[0], roi.y+location[1], w, h),
                                             score_value, anchor.threshold)
                    else:
                        result = match_template(scaled, profile, anchor.png, cache=self.cache)
                    score = min(score, result.score)
                    region = result.region
                    patch = scaled.gray[region.y:region.y + region.height, region.x:region.x + region.width]
                    # Normalized correlation also matches dimmed buttons behind
                    # modal dialogs. Require appearance to agree in brightness.
                    error = np.abs(patch.astype(np.float32) - template.astype(np.float32))
                    pixel_error = float(error[mask > 0].mean() if mask is not None else error.mean())
                    valid = valid and result.matched and pixel_error <= anchor.max_pixel_error
                    sx, sy = frame.width / width, frame.height / height
                    x, y = round(region.x * sx), round(region.y * sy)
                    right = min(frame.width, math.ceil((region.x + region.width) * sx))
                    bottom = min(frame.height, math.ceil((region.y + region.height) * sy))
                    hits.append(AnchorHit(anchor.name, Region(x, y, right - x, bottom - y), result.score))
                    # A node requires every anchor. Still examine all other
                    # nodes so dialogs and ambiguous matches remain visible.
                    if not valid:
                        break
            candidates.append((node.name, score))
            if valid:
                matched.append(Recognition(node.name, node.label, score, tuple(hits), frame.frame_id))
        if len(matched) == 1:
            item = matched[0]
            result = Recognition(item.state, item.label, item.score, item.hits, frame.frame_id, tuple(candidates))
        else:
            label = "多个界面特征同时命中" if matched else "未知/过渡界面"
            result = Recognition("", label, max((score for _, score in candidates), default=0), (), frame.frame_id, tuple(candidates))
        self._last_frame, self._last_result = frame, result
        return result
