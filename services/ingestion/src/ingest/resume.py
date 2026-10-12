"""Where a re-run can start (brief §10, resume logic).

The last SUCCEEDED step of the article's previous run whose staged output still has the ETag it
recorded: the run starts at the step after it. Fetch writes nothing to staging (it verifies the
producer's versioned raw object, which Extract reads and verifies again), so a SUCCEEDED Fetch is
enough to start at Extract. Index and Finalize are idempotent, so a run that got past Validate
restarts at Index. Without a usable step it falls back to Fetch (None).
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError

from ingest import aws
from ingest.models.status import StepName, StepStatus
from ingest.staging.io import parse_s3_uri
from ingest.status import keys
from ingest.status.store import Item, StatusStore

# Steps that write staged output, in pipeline order, and the step that consumes it.
_NEXT: dict[StepName, StepName] = {
    StepName.FETCH: StepName.EXTRACT,
    StepName.EXTRACT: StepName.NORMALIZE,
    StepName.NORMALIZE: StepName.CHUNK,
    StepName.CHUNK: StepName.EMBED,
    StepName.EMBED: StepName.VALIDATE,
    StepName.VALIDATE: StepName.INDEX,
}


def _etag(uri: str) -> str | None:
    bucket, key = parse_s3_uri(uri)
    try:
        head: Any = aws.s3().head_object(Bucket=bucket, Key=key)
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise
    return str(head["ETag"]).strip('"')


def resume_stage(
    store: StatusStore, latest: Item | None, content_sha256: str | None
) -> StepName | None:
    """The step to resume at for this content, or None to start at Fetch."""
    if not latest or not latest.get("run_id") or latest.get("content_sha256") != content_sha256:
        return None  # new content: staged files belong to another version
    steps = {
        str(i["sk"]): i
        for i in store.run_items(str(latest["pk"]), str(latest["run_id"]))
        if "#STEP#" in str(i["sk"])
    }
    for step in reversed(list(_NEXT)):
        item = steps.get(keys.step_sk(str(latest["run_id"]), step))
        if not item or item.get("step_status") != StepStatus.SUCCEEDED:
            continue
        if step is StepName.FETCH:
            return _NEXT[step]
        uri, etag = item.get("output_uri"), item.get("output_etag")
        if uri and etag and _etag(str(uri)) == str(etag).strip('"'):
            return _NEXT[step]
    return None
