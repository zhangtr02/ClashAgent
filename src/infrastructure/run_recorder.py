import json
from datetime import datetime
from dataclasses import asdict
from pathlib import Path

from core.flow.models import FlowEvent
from infrastructure.settings import PROJECT_ROOT


class RunRecorder:
    def __init__(self, run_id: str, root: Path | None = None):
        self.directory = (root or PROJECT_ROOT / "logs" / "runs") / run_id
        self.directory.mkdir(parents=True, exist_ok=True)

    def metadata(self, value: dict) -> None:
        (self.directory / "run.json").write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def record(self, event: FlowEvent) -> None:
        # Live preview still receives every frame; disk logs retain transitions
        # and actions rather than repeating candidate scores every polling tick.
        if event.kind == "observation":
            return
        record = {"time": datetime.now().astimezone().isoformat(), "run_id": event.run_id,
                  "kind": event.kind, "phase": event.phase, "node": event.node, "message": event.message}
        record.update(cycle=event.cycle, cycles_completed=event.cycles_completed, deployments=event.deployments)
        record["verified_batches"] = event.verified_batches
        if event.details:
            record["details"] = event.details
        if event.resources:
            record["resources"] = [asdict(reading) for reading in event.resources]
        if event.frame:
            record["frame_id"] = event.frame.frame_id
            record["backend"] = event.frame.backend
        if event.recognition:
            record["recognized_state"] = event.recognition.state
            record["score"] = event.recognition.score
            record["candidates"] = dict(event.recognition.candidates)
            record["hits"] = [{"name": hit.name, "score": hit.score,
                               "rect": [hit.region.x, hit.region.y, hit.region.width, hit.region.height]}
                              for hit in event.recognition.hits]
        if event.frame and (event.kind == "terminal" or (event.kind == "phase" and event.phase == "paused")):
            name = f"{event.kind}-{event.node or 'unknown'}-{event.frame.frame_id[:8]}.png"
            (self.directory / name).write_bytes(event.frame.png)
            record["screenshot"] = name
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
