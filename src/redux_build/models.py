from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum


class Status(StrEnum):
    success = "success"
    failure = "failure"
    skipped = "skipped"
    warning = "warning"


@dataclass
class Fragment:
    engine: str
    operation: str
    status: Status
    summary: str = ""
    variant: str = ""
    metrics: dict = field(default_factory=dict)
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status in (Status.success, Status.skipped, Status.warning)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.value
        return data
