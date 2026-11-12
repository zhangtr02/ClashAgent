import cv2

from core.capture import decode_png
from core.flow.builder_vision import BuilderVision
from infrastructure.flow_resources import load_flow
from infrastructure.resource_reader import load_resource_reader
from infrastructure.settings import PROJECT_ROOT

def load_builder_battle():
    root = PROJECT_ROOT / "resources/flows"
    definition = load_flow(root / "builder_battle.json")
    reader = load_resource_reader(builder=True)
    witch = decode_png((root / "builder/witch.png").read_bytes())
    second_stage = decode_png((root / "builder/stage-two.png").read_bytes())
    return definition, BuilderVision(reader, cv2.cvtColor(witch, cv2.COLOR_BGR2GRAY),
                                     cv2.cvtColor(second_stage, cv2.COLOR_BGR2GRAY))
