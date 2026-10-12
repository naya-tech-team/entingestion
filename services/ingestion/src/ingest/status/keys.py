"""Key layout of the single ingestion_status table (brief §6.1-6.2)."""

from __future__ import annotations

from ingest.models.status import ArticleStatus, StepName

LATEST = "LATEST"
SUMMARY = "SUMMARY"
LEASE_BUCKET = "LEASE"
# gsi1 partition of SUMMARY items whose process may still need completing (reconciler §11.5).
SUMMARY_OPEN = "SUMMARY#OPEN"


def run_sk(run_id: str) -> str:
    return f"RUN#{run_id}"


def step_sk(run_id: str, step: StepName) -> str:
    return f"RUN#{run_id}#{step.item_suffix}"


def process_pk(process_id: str) -> str:
    return f"PROCESS#{process_id}"


def member_sk(article_id: str) -> str:
    return f"ARTICLE#{article_id}"


def latest_key(article_id: str) -> dict[str, str]:
    return {"pk": article_id, "sk": LATEST}


def shadow_key(article_id: str, table: str) -> dict[str, str]:
    """Status of a run that writes one vector table without being the article's live run
    (reindex, rebuild, drift repair). It has no GSI keys and no process roll-up."""
    return {"pk": article_id, "sk": f"SHADOW#{table}"}


def run_key(article_id: str, run_id: str) -> dict[str, str]:
    return {"pk": article_id, "sk": run_sk(run_id)}


def step_key(article_id: str, run_id: str, step: StepName) -> dict[str, str]:
    return {"pk": article_id, "sk": step_sk(run_id, step)}


def summary_key(process_id: str) -> dict[str, str]:
    return {"pk": process_pk(process_id), "sk": SUMMARY}


def member_key(process_id: str, article_id: str) -> dict[str, str]:
    return {"pk": process_pk(process_id), "sk": member_sk(article_id)}


# gsi4 also lists LATEST items with a delete waiting for the running run to hand over.
DELETE_PENDING = "DELETE#PENDING"


def stepfail_pk(step: StepName) -> str:
    return f"STEPFAIL#{step.value}"


def gsi2_sk(status: ArticleStatus, article_id: str) -> str:
    return f"{status.value}#{article_id}"


# Small configuration and cursor items (vector-table registry, sweep cursors).
CONFIG = "#CONFIG"


def config_key(name: str) -> dict[str, str]:
    return {"pk": CONFIG, "sk": name}


# Admission slots (dispatcher backpressure): one item per slot, sk <lane>#<i>, holding the run
# that owns it until the run ends or the slot's lease runs out. A missing item is a free slot,
# so nothing needs seeding and changing a lane's size takes effect at once.
SLOTS = "#SLOTS"


def slot_sk(lane: str, index: int) -> str:
    return f"{lane}#{index}"


def slot_key(slot: str) -> dict[str, str]:
    return {"pk": SLOTS, "sk": slot}
