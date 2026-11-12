"""Visual evidence from the numbered Builder Base deployment cards."""
from dataclasses import dataclass

import cv2
import numpy as np

from core.flow.deployment import CardDetector
from core.resources import white_mask
from core.vision.models import Region


@dataclass(frozen=True)
class BuilderCard:
    slot: int
    x: int
    status: str
    selected: bool = False
    skill_ready: bool = False

    @property
    def label(self):
        return f"女巫 {self.slot}" if self.slot else "英雄"


class BuilderVision:
    WITCH_CENTERS = (243, 344, 445, 546, 647, 749, 850, 947)
    HERO = Region(84, 595, 89, 116)

    def __init__(self, reader, witch, stage_two):
        self.reader = reader
        self.witch = tuple(cv2.resize(witch, None, fx=s, fy=s, interpolation=cv2.INTER_LINEAR)
                           for s in (1, 1.04, 1.08))
        self.stage_two = stage_two

    def second_preview(self, frame):
        patch = self.reader.pixels(frame)[484:520, 62:122]
        gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
        return float(cv2.matchTemplate(gray, self.stage_two, cv2.TM_CCOEFF_NORMED).max()) >= .91

    @staticmethod
    def _witch_selected(pixels, x):
        border = white_mask(pixels[576:699, x-51:x+52])
        top = border[:12].sum(axis=1).max() >= 65
        left = border[17:, :10].any(axis=1).mean()
        right = border[17:, -10:].any(axis=1).mean()
        return bool(top and min(left, right) >= .85)

    @staticmethod
    def _hero_deployed(pixels):
        # After deployment the health track moves above the unused card.
        # Its frame stays fixed even when the fill is short or portrait
        # colors show through the unfilled portion.
        track = pixels[568:582, 91:167]
        dark = cv2.cvtColor(track, cv2.COLOR_BGR2GRAY) < 65
        if dark[[0, 1, -2, -1]].mean(axis=1).min() < .88:
            return False

        # Read only the left end inside the track, away from the ability
        # marker and the portrait. Require a solid fill that starts there.
        inset = track[4:9, :6]
        hue, saturation, value = cv2.split(cv2.cvtColor(inset, cv2.COLOR_BGR2HSV))
        fill = ((hue <= 90) | (hue >= 174)) & (saturation > 100) & (value > 95)
        solid_columns = np.count_nonzero(fill, axis=0) >= 4
        return any(solid_columns[start:start+3].all() for start in range(4))

    def cards(self, frame, *, tracked_slots=()):
        pixels = self.reader.pixels(frame)
        gray = cv2.cvtColor(pixels[545:715], cv2.COLOR_BGR2GRAY)
        hsv = cv2.cvtColor(pixels[545:715], cv2.COLOR_BGR2HSV)
        cards = {}
        for slot, x in enumerate(self.WITCH_CENTERS, 1):
            badge = pixels[568:610, x-43:x-13]
            number = self.reader.read_number(badge)
            # Selection can connect the white frame to the number glyph.
            # Only recover an unreadable badge in an already identified slot.
            badge_obscured = number is None and slot in tracked_slots and self._witch_selected(pixels, x)
            if not badge_obscured and (number is None or number[0] != slot):
                continue
            top = self.reader.read_number(badge[:20])
            selected = self.reader.read_number(badge[12:34])
            deployed = not badge_obscured and top is not None and top[0] == slot
            is_selected = not deployed and (badge_obscured or (selected is not None and selected[0] == slot))
            face = gray[78:132, x-35:x+35]
            score = max(float(cv2.matchTemplate(face, sample, cv2.TM_CCOEFF_NORMED).max())
                        for sample in self.witch)
            if score < (.90 if badge_obscured else .78):
                cards[slot] = BuilderCard(slot, x, "unknown", is_selected)
                continue
            portrait = hsv[88:121, x-22:x+22]
            if float(np.mean(portrait[:, :, 1] > 45)) < .08:
                cards[slot] = BuilderCard(slot, x, "dead")
                continue
            # The ability strip sits immediately above the health bar once
            # deployed. A dark strip on a survivor is already spent.
            strip = hsv[28:49, x+1:x+32] if deployed else hsv[43:70, x+1:x+32]
            purple = ((strip[:, :, 0] >= 130) & (strip[:, :, 0] <= 155)
                      & (strip[:, :, 1] > 105) & (strip[:, :, 2] > 185))
            ready = float(np.mean(purple)) > .40
            cards[slot] = BuilderCard(slot, x, "deployed" if deployed else "available", is_selected, ready)

        # Hero machine slots have a purple border, unlike troop slots and
        # empty dashed placeholders. Only click it to select before launch.
        hero = hsv[138:165, 85:173]
        purple_border = ((hero[:, :, 0] >= 117) & (hero[:, :, 0] <= 151)
                         & (hero[:, :, 1] > 105) & (hero[:, :, 2] > 105))
        if np.count_nonzero(purple_border) > 60:
            deployed = self._hero_deployed(pixels)
            selected = CardDetector.hero_selected(pixels, self.HERO)
            cards[0] = BuilderCard(0, 128, "deployed" if deployed else "available", selected)
        return cards
