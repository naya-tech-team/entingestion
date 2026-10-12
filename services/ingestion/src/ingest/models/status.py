"""Pipeline steps and status values stored in ingestion_status (brief §4.4, §6)."""

from __future__ import annotations

from enum import StrEnum


class StepName(StrEnum):
    CLAIM = "CLAIM"
    FETCH = "FETCH"
    EXTRACT = "EXTRACT"
    NORMALIZE = "NORMALIZE"
    CHUNK = "CHUNK"
    EMBED = "EMBED"
    VALIDATE = "VALIDATE"
    INDEX = "INDEX"
    FINALIZE = "FINALIZE"

    @property
    def number(self) -> int:
        return _ORDER.index(self) + 1

    @property
    def item_suffix(self) -> str:
        """Sort-key suffix of the step item, e.g. STEP#03_EXTRACT."""
        return f"STEP#{self.number:02d}_{self.value}"

    @property
    def previous(self) -> StepName | None:
        i = _ORDER.index(self)
        return _ORDER[i - 1] if i > 0 else None


_ORDER: tuple[StepName, ...] = tuple(StepName)


class ArticleStatus(StrEnum):
    """LATEST.status."""

    CLAIMED = "CLAIMED"
    FETCHING = "FETCHING"
    EXTRACTING = "EXTRACTING"
    NORMALIZING = "NORMALIZING"
    CHUNKING = "CHUNKING"
    EMBEDDING = "EMBEDDING"
    VALIDATING = "VALIDATING"
    STAGED = "STAGED"  # validated and ready to index
    INDEXING = "INDEXING"
    INDEXED = "INDEXED"
    SKIPPED_UNCHANGED = "SKIPPED_UNCHANGED"
    SKIPPED = "SKIPPED"  # nothing to index, e.g. E_EMPTY_TEXT
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_PERMANENT = "FAILED_PERMANENT"
    DELETED = "DELETED"


# Status LATEST shows while each step runs. Validate moves to STAGED when the manifest passes,
# and Finalize to INDEXED (brief §4.4 "after" column).
STEP_STATUS: dict[StepName, ArticleStatus] = {
    StepName.CLAIM: ArticleStatus.CLAIMED,
    StepName.FETCH: ArticleStatus.FETCHING,
    StepName.EXTRACT: ArticleStatus.EXTRACTING,
    StepName.NORMALIZE: ArticleStatus.NORMALIZING,
    StepName.CHUNK: ArticleStatus.CHUNKING,
    StepName.EMBED: ArticleStatus.EMBEDDING,
    StepName.VALIDATE: ArticleStatus.VALIDATING,
    StepName.INDEX: ArticleStatus.INDEXING,
    StepName.FINALIZE: ArticleStatus.INDEXING,
}

# Status set when a step succeeds, where it differs from the running status.
STEP_SUCCESS_STATUS: dict[StepName, ArticleStatus] = {
    StepName.VALIDATE: ArticleStatus.STAGED,
}

# Process roll-up counter for each terminal status (brief §6.4).
ROLLUP_COUNTER: dict[ArticleStatus, str] = {
    ArticleStatus.INDEXED: "indexed",
    ArticleStatus.SKIPPED: "skipped",
    ArticleStatus.SKIPPED_UNCHANGED: "skipped",
    ArticleStatus.FAILED_PERMANENT: "failed",
}

# Moving into one of these updates the process roll-up (brief §6.4).
TERMINAL_STATUSES: frozenset[ArticleStatus] = frozenset(
    {
        ArticleStatus.INDEXED,
        ArticleStatus.SKIPPED_UNCHANGED,
        ArticleStatus.SKIPPED,
        ArticleStatus.FAILED_PERMANENT,
        ArticleStatus.DELETED,
    }
)


class StepStatus(StrEnum):
    STARTED = "STARTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ClaimKind(StrEnum):
    NEW = "NEW"
    CHANGED = "CHANGED"
    UNCHANGED = "UNCHANGED"
    METADATA = "METADATA"  # same content and title, other metadata or tenant: updated in place
    DELETE = "DELETE"


class RunOutcome(StrEnum):
    INDEXED = "INDEXED"
    SKIPPED_UNCHANGED = "SKIPPED_UNCHANGED"
    METADATA_UPDATED = "METADATA_UPDATED"  # counted as skipped, like an unchanged article
    SKIPPED = "SKIPPED"
    DELETED = "DELETED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_PERMANENT = "FAILED_PERMANENT"
    STALE = "STALE"  # lost the lease to another run; ended quietly
    SUPERSEDED = "SUPERSEDED"  # older than what the article already has; ended quietly
    DELETE_DEFERRED = "DELETE_DEFERRED"  # a delete arrived mid-run; the run hands it over
    PREEMPTED = "PREEMPTED"  # handed the article over to a pending delete
