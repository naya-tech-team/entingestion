"""What the status store hands out: items, claim decisions and run references."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from ingest.models.status import (
    ClaimKind,
    StepName,
)

if TYPE_CHECKING:
    pass


Item = dict[str, Any]


MAX_ATTEMPTS_KEPT = 5


@dataclass(frozen=True)
class ClaimResult:
    action: Literal["RUN", "SKIP", "STALE", "DELETE", "DONE", "METADATA"]
    claim: ClaimKind | None = None
    doc_version: int | None = None
    start_at: StepName | None = None
    staging_prefix: str | None = None
    lane: str | None = None
    targets: tuple[str, ...] = ()


@dataclass(frozen=True)
class StepRef:
    """Identifies the run a step belongs to. lane: the vector table of a shadow run (its status
    lives on SHADOW#<table>, and it may only work on LATEST's doc_version). slot: the admission
    slot the dispatcher gave the run (#SLOTS/<slot>), released when the run ends."""

    article_id: str
    run_id: str
    process_id: str | None
    lane: str | None = None
    doc_version: int | None = None
    slot: str | None = None
