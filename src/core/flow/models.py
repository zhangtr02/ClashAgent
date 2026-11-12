from dataclasses import dataclass, field

from core.capture import ScreenFrame
from core.resources import ResourceReading
from core.vision.models import Region


@dataclass(frozen=True)
class Anchor:
    name: str
    png: bytes = field(repr=False)
    roi: Region
    threshold: float = 0.9
    max_pixel_error: float = 25
    text_mask: bool = False
    mask_png: bytes | None = field(default=None, repr=False)


@dataclass(frozen=True)
class FlowNode:
    name: str
    label: str
    anchors: tuple[Anchor, ...]
    action: str
    target: str = ""
    next: tuple[str, ...] = ()
    timeout: float = 10
    retry_interval: float = 3
    max_attempts: int = 2
    algorithm: str = "template"
    message: str = ""


@dataclass(frozen=True)
class FlowDefinition:
    name: str
    label: str
    reference_width: int
    reference_height: int
    nodes: tuple[FlowNode, ...]
    start: tuple[str, ...]
    stable_frames: int = 2
    poll_interval: float = 0.2
    unknown_timeout: float = 8
    max_actions: int = 8

    def node(self, name: str) -> FlowNode:
        return next(node for node in self.nodes if node.name == name)


@dataclass(frozen=True)
class AnchorHit:
    name: str
    region: Region
    score: float


@dataclass(frozen=True)
class Recognition:
    state: str
    label: str
    score: float
    hits: tuple[AnchorHit, ...]
    frame_id: str
    candidates: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class FlowEvent:
    kind: str
    phase: str
    node: str = ""
    message: str = ""
    frame: ScreenFrame | None = field(default=None, repr=False)
    recognition: Recognition | None = None
    run_id: str = ""
    cycle: int = 0
    cycles_completed: int = 0
    deployments: int = 0
    verified_batches: int = 0
    resources: tuple[ResourceReading, ...] = ()
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class FlowResult:
    phase: str
    node: str
    message: str
    run_id: str
    cycles_completed: int = 0
