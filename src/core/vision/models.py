from dataclasses import dataclass

from core.errors import ClashAgentError


@dataclass(frozen=True)
class Region:
    x: int
    y: int
    width: int
    height: int

    def validate(self, frame_width: int, frame_height: int) -> None:
        if any(type(value) is not int for value in (self.x, self.y, self.width, self.height)):
            raise ClashAgentError("区域坐标必须为整数。")
        if (self.x < 0 or self.y < 0 or self.width < 1 or self.height < 1
                or self.x + self.width > frame_width or self.y + self.height > frame_height):
            raise ClashAgentError("区域超出截图边界，或宽高为空。")


@dataclass(frozen=True)
class TemplateProfile:
    template_id: str
    name: str
    frame_width: int
    frame_height: int
    crop: Region
    search_region: Region | None = None
    threshold: float = 0.9


@dataclass(frozen=True)
class MatchResult:
    template_id: str
    frame_id: str
    region: Region
    score: float
    threshold: float

    @property
    def matched(self) -> bool:
        return self.score >= self.threshold
