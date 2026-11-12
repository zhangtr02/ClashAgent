"""Fixed candidates become usable only after an individually confirmed probe."""
from dataclasses import dataclass

from core.errors import ClashAgentError


SIDES = ("左上", "右上", "左下", "右下")
# All points are in the normalized 1280x720 camera view and outside HUD/skills.
CANDIDATES = (
    ((325, 200), (285, 230), (245, 260), (205, 290)),
    ((955, 200), (995, 230), (1035, 260), (1075, 290)),
    ((285, 435), (325, 470), (245, 400), (365, 490)),
    ((995, 435), (955, 470), (1035, 400), (915, 490)),
)


@dataclass(frozen=True)
class DropPoint:
    side: int
    candidate: int
    x: int
    y: int

    def details(self):
        return {"side": SIDES[self.side], "candidate": self.candidate+1, "point": [self.x, self.y]}


class DeploymentMapSession:
    def __init__(self):
        self.cursor = [0]*4
        self.verified = {}
        self.rejected = set()

    def next_probe(self):
        for side, candidates in enumerate(CANDIDATES):
            if side not in self.verified and self.cursor[side] < len(candidates):
                index = self.cursor[side]
                return DropPoint(side, index, *candidates[index])
        return None

    @property
    def probing(self):
        return self.next_probe() is not None

    @property
    def points(self):
        return tuple(self.verified[side] for side in sorted(self.verified))

    def resolve(self, point, success):
        if self.next_probe() != point:
            raise ClashAgentError("试投落点与当前候选点不一致。")
        self.cursor[point.side] = point.candidate+1
        if success:
            self.verified[point.side] = point
        else:
            self.rejected.add(point)

    def reject(self, points):
        # A failed batch does not tell which tap failed. Withdraw every point
        # used by that batch; subsequent inputs require other proven points.
        for point in set(points):
            self.rejected.add(point)
            if self.verified.get(point.side) == point:
                self.verified.pop(point.side)
                self.cursor[point.side] = len(CANDIDATES[point.side])

    def finish_probing(self):
        for side in range(4):
            if side not in self.verified:
                self.cursor[side] = len(CANDIDATES[side])
        if not self.points:
            raise ClashAgentError("没有已验证的合法落点，且没有普通兵可继续试投。")

    def require_points(self):
        if not self.points:
            raise ClashAgentError("所有候选点均未通过实际试投，已阻止批量下兵。")
        return self.points

    def details(self):
        return {"verified": [p.details() for p in self.points],
                "rejected": [p.details() for p in sorted(self.rejected, key=lambda p: (p.side, p.candidate))],
                "remaining_probe": self.next_probe().details() if self.probing else None}
