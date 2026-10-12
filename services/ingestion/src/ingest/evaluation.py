"""Retrieval evaluation against a golden set (review M3.4).

The golden set is versioned JSON lines in staging, _eval/golden/<YYYYMMDD-HHMM>.jsonl, and
_eval/golden/LATEST names the current one (scripts/make_golden_set.py writes both and never
overwrites a set). Each line:

    {"id": ..., "question": ..., "article_id": ..., "doc_version": 3,
     "chunk_ids": ["..."], "no_answer": false}

no_answer questions have no relevant chunk; they're reported apart (how high the best score
goes for a question the corpus can't answer). A set from before chunk labels (_eval/golden.jsonl,
article ids only) still gives the article-level score.

The eval Lambda embeds each question (as a query) with the evaluated table's embedding profile,
asks the query function (inside the VPC) for the nearest 50 chunks, and scores:

    recall_at_5, recall_at_10   share of a question's relevant chunks in the top k (mean)
    ndcg_at_10                  binary-relevance nDCG over the top 10
    mrr                         1 / rank of the first relevant chunk (0 if none in 50)
    article_recall_at_10        the expected article among the first 10 distinct articles

each with a bootstrap 95% confidence interval over questions. Questions whose article is
deleted or no longer indexed are dropped, and so are those labelled for an older version of
their article (its chunk ids changed): both are counted in the report.

    {"table": null}                                 the live table (chunks_active)
    {"table": "chunks_compact"}                  a reindexed table, before switching to it
    {"table": "chunks_compact", "baseline": true}  and the live table, for comparison

Reports go to _eval/<table>/<YYYYMMDDTHHMMSSZ>.json (the switch gate reads the newest) and the
scores to CloudWatch (namespace Ingestion).
"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import ClientError

from ingest import aws
from ingest.config import get_settings
from ingest.embedding.factory import make_embedder
from ingest.models.status import ArticleStatus
from ingest.search import call_query
from ingest.vectorstore.targets import target

logger = Logger()
metrics = Metrics(namespace="Ingestion")  # same as POWERTOOLS_METRICS_NAMESPACE

GOLDEN_DIR = "_eval/golden/"
GOLDEN_POINTER = f"{GOLDEN_DIR}LATEST"
LEGACY_GOLDEN = "_eval/golden.jsonl"
CHUNKS_PER_QUERY = 50  # enough distinct articles for recall@10
_SEARCH_BATCH = 20
BOOTSTRAP_SAMPLES = 1000
BOOTSTRAP_SEED = 7
METRICS = ("recall_at_5", "recall_at_10", "ndcg_at_10", "mrr", "article_recall_at_10")


def report_prefix(table: str) -> str:
    return f"_eval/{table}/"


# -- scoring ----------------------------------------------------------------------------------


def article_ranking(hits: Sequence[dict[str, Any]]) -> list[str]:
    """Articles in order of their best chunk."""
    return list(dict.fromkeys(str(h["article_id"]) for h in hits))


@dataclass(frozen=True)
class Judged:
    """One answerable question's scores."""

    recall_at_5: float
    recall_at_10: float
    ndcg_at_10: float
    mrr: float
    article_recall_at_10: float
    labelled: bool = True  # chunk labels (False: an article-only legacy question)


def judge(hits: Sequence[dict[str, Any]], relevant: set[str], article_id: str) -> Judged:
    ranked = [str(h["chunk_id"]) for h in hits]
    articles = article_ranking(hits)
    article_hit = 1.0 if article_id in articles[:10] else 0.0
    if not relevant:  # legacy line: article-level only
        rank = articles.index(article_id) + 1 if article_id in articles else None
        return Judged(
            recall_at_5=1.0 if article_id in articles[:5] else 0.0,
            recall_at_10=article_hit,
            ndcg_at_10=article_hit / math.log2((rank or 1) + 1) if rank and rank <= 10 else 0.0,  # noqa: PLR2004
            mrr=1 / rank if rank else 0.0,
            article_recall_at_10=article_hit,
            labelled=False,
        )
    found = [i for i, c in enumerate(ranked, start=1) if c in relevant]
    dcg = sum(1 / math.log2(r + 1) for r in found if r <= 10)  # noqa: PLR2004
    ideal = sum(1 / math.log2(r + 1) for r in range(1, min(len(relevant), 10) + 1))
    return Judged(
        recall_at_5=sum(1 for r in found if r <= 5) / len(relevant),  # noqa: PLR2004
        recall_at_10=sum(1 for r in found if r <= 10) / len(relevant),  # noqa: PLR2004
        ndcg_at_10=dcg / ideal,
        mrr=1 / found[0] if found else 0.0,
        article_recall_at_10=article_hit,
    )


def bootstrap_ci(
    values: Sequence[float], samples: int = BOOTSTRAP_SAMPLES, seed: int = BOOTSTRAP_SEED
) -> tuple[float, float]:
    """95% percentile bootstrap interval of the mean."""
    if not values:
        return (0.0, 0.0)
    rng = random.Random(seed)  # noqa: S311 - resampling, not security
    n = len(values)
    means = sorted(sum(rng.choices(values, k=n)) / n for _ in range(samples))
    return (means[int(0.025 * samples)], means[min(int(0.975 * samples), samples - 1)])


def summarize(judged: Sequence[Judged]) -> dict[str, Any]:
    out: dict[str, Any] = {"questions": len(judged)}
    for name in METRICS:
        values = [float(getattr(j, name)) for j in judged]
        low, high = bootstrap_ci(values)
        out[name] = sum(values) / len(values) if values else 0.0
        out[f"{name}_ci95"] = [round(low, 4), round(high, 4)]
    return out


# -- golden set -------------------------------------------------------------------------------


def load_golden(read: Callable[[str], bytes | None]) -> tuple[str, list[dict[str, Any]]]:
    """(key, questions) of the current golden set: the one LATEST names, else the legacy file."""
    pointer = read(GOLDEN_POINTER)
    key = pointer.decode().strip() if pointer else LEGACY_GOLDEN
    body = read(key)
    if body is None:
        raise FileNotFoundError(f"no golden set at {key}; run scripts/make_golden_set.py")
    return key, [json.loads(line) for line in body.decode().splitlines() if line.strip()]


def _read_staging(key: str) -> bytes | None:
    try:
        obj = aws.s3().get_object(Bucket=get_settings().staging_bucket, Key=key)
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") in {"NoSuchKey", "404", "NotFound"}:
            return None
        raise
    return obj["Body"].read()


def current(questions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """The questions still answerable as labelled: their article is indexed (not deleted) and,
    for chunk labels, still at the labelled version."""
    store = aws.status_store()
    latest: dict[str, dict[str, Any] | None] = {}
    kept, dropped = [], {"deleted": 0, "stale": 0}
    for q in questions:
        if q.get("no_answer"):
            kept.append(q)
            continue
        article = str(q["article_id"])
        if article not in latest:
            latest[article] = store.latest(article)
        item = latest[article] or {}
        if item.get("status") != ArticleStatus.INDEXED:
            dropped["deleted"] += 1
        elif q.get("chunk_ids") and int(q.get("doc_version") or 0) != int(item["doc_version"]):
            dropped["stale"] += 1
        else:
            kept.append(q)
    return kept, dropped


# -- run --------------------------------------------------------------------------------------


@dataclass
class Evaluation:
    table: str
    live: bool
    report: dict[str, Any] = field(default_factory=dict)
    key: str = ""


def evaluate(
    table: str | None, questions: list[dict[str, Any]], golden_key: str, dropped: dict[str, int]
) -> Evaluation:
    settings = get_settings()
    name = table or call_query({"action": "describe"})["active_table"]
    t = target(str(name))
    model = make_embedder(t.profile)
    with ThreadPoolExecutor(max_workers=max(1, settings.embed_concurrency)) as pool:
        vectors = list(pool.map(lambda g: model.embed_query(str(g["question"])).vector, questions))
    results: list[list[dict[str, Any]]] = []
    for i in range(0, len(vectors), _SEARCH_BATCH):
        out = call_query(
            {
                "action": "search",
                "vectors": vectors[i : i + _SEARCH_BATCH],
                "k": CHUNKS_PER_QUERY,
                "table": table,
            }
        )
        results += out["results"]
    judged: list[Judged] = []
    misses: list[dict[str, Any]] = []
    top_answer: list[float] = []
    top_none: list[float] = []
    for q, hits in zip(questions, results, strict=True):
        best = float(hits[0]["score"]) if hits and "score" in hits[0] else 0.0
        if q.get("no_answer"):
            top_none.append(best)
            continue
        top_answer.append(best)
        j = judge(hits, set(q.get("chunk_ids") or []), str(q["article_id"]))
        judged.append(j)
        if j.recall_at_10 == 0 and len(misses) < 50:  # noqa: PLR2004
            misses.append(
                {
                    "question": q["question"],
                    "expected": q["article_id"],
                    "top": article_ranking(hits)[:5],
                }
            )
    at = datetime.now(UTC)
    report = {
        "table": t.table,
        "live": table is None,
        "model": t.model_id,
        "dimension": t.dimension,
        "at": at.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "golden": golden_key,
        "dropped": dropped,
        "chunk_labelled": sum(1 for j in judged if j.labelled),
        **summarize(judged),
        "no_answer": {
            "questions": len(top_none),
            "mean_top_score": sum(top_none) / len(top_none) if top_none else None,
            "answerable_mean_top_score": sum(top_answer) / len(top_answer) if top_answer else None,
        },
        "misses": misses,
    }
    key = f"{report_prefix(t.table)}{at.strftime('%Y%m%dT%H%M%SZ')}.json"
    aws.s3().put_object(
        Bucket=settings.staging_bucket,
        Key=key,
        Body=json.dumps(report, indent=2).encode(),
        ContentType="application/json",
    )
    return Evaluation(t.table, table is None, report, key)


def latest_report(table: str) -> dict[str, Any] | None:
    """The newest evaluation report of a table (keys sort by time), or None."""
    bucket = get_settings().staging_bucket
    newest: str | None = None
    for page in (
        aws.s3()
        .get_paginator("list_objects_v2")
        .paginate(Bucket=bucket, Prefix=report_prefix(table))
    ):
        for obj in page.get("Contents", []):
            key = str(obj["Key"])
            if key.endswith(".json") and (newest is None or key > newest):
                newest = key
    if newest is None:
        return None
    body = json.loads(aws.s3().get_object(Bucket=bucket, Key=newest)["Body"].read())
    return dict(body) | {"key": newest}


def _missing_or_old(
    table: str, report: dict[str, Any] | None, max_age_days: int | None, now: datetime
) -> dict[str, Any] | None:
    if report is None:
        return {"message": f"{table} has no evaluation report"}
    at = datetime.fromisoformat(str(report["at"]).replace("Z", "+00:00"))
    if max_age_days is not None and (now - at).days >= max_age_days:
        days = (now - at).days
        return {
            "message": f"{table}'s newest evaluation is {days} days old (max {max_age_days})",
            "report": report["key"],
        }
    return None


def switch_gate(
    table: str, live: str, *, tolerance: float, max_age_days: int, now: datetime | None = None
) -> dict[str, Any] | None:
    """Why switching chunks_active from `live` to `table` is refused, or None. The table's
    newest report must be at most max_age_days old, from the same golden set as the live
    table's newest report, with recall@10 no more than `tolerance` below it."""
    if table == live:
        return None
    now = now or datetime.now(UTC)
    mine, theirs = latest_report(table), latest_report(live)
    problem = _missing_or_old(table, mine, max_age_days, now) or _missing_or_old(
        live, theirs, None, now
    )
    if problem is None and mine is not None and theirs is not None:
        if mine.get("golden") != theirs.get("golden"):
            problem = {
                "message": "the two tables were evaluated on different golden sets",
                "golden": {table: mine.get("golden"), live: theirs.get("golden")},
            }
        elif float(mine["recall_at_10"]) < float(theirs["recall_at_10"]) - tolerance:
            return {
                "message": f"{table} recall@10 {mine['recall_at_10']:.3f} is more than"
                f" {tolerance} below the live table's {theirs['recall_at_10']:.3f}",
                "recall_at_10": {table: mine["recall_at_10"], live: theirs["recall_at_10"]},
                "reports": {table: mine["key"], live: theirs["key"]},
            }
    if problem is not None:
        return problem | {"hint": f"run scripts/evaluate.py --table {table} --baseline"}
    return None


def _publish(e: Evaluation) -> None:
    metrics.add_dimension(name="table", value=e.table)
    for name in ("recall_at_5", "recall_at_10", "ndcg_at_10", "mrr"):
        # As percentages (Powertools has no unitless unit): recall@5 0.8 -> 80.
        metrics.add_metric(
            name=f"Eval_{name}", unit=MetricUnit.Percent, value=round(100 * e.report[name], 2)
        )


def _summary(e: Evaluation) -> dict[str, Any]:
    settings = get_settings()
    return {k: v for k, v in e.report.items() if k != "misses"} | {
        "report": f"s3://{settings.staging_bucket}/{e.key}"
    }


@logger.inject_lambda_context(clear_state=True)
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    golden_key, questions = load_golden(_read_staging)
    questions, dropped = current(questions)
    table = event.get("table")
    main = evaluate(table, questions, golden_key, dropped)
    _publish(main)
    out = _summary(main)
    if event.get("baseline") and not main.live:
        live = evaluate(None, questions, golden_key, dropped)
        if live.table != main.table:
            out["baseline"] = _summary(live)
            out["delta"] = {m: main.report[m] - live.report[m] for m in METRICS}
    logger.info("evaluated", extra=out)
    return out
