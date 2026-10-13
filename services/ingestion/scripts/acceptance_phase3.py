"""Phase 3 checks against a deployed environment.

    uv run python scripts/acceptance_phase3.py uc12 [--pages 3] [--env dev]

UC12: a scanned PDF (page images, no text layer) is OCR'd by Textract and INDEXED without a
Lambda timeout. A small one takes the Extract -> OCR hand-off; one over the Lambda size limit
(LAMBDA_MAX_SOURCE_BYTES, 50 MB) goes straight from Fetch to OCR.
Textract bills per page (about $1.50 per 1,000 pages for text detection).
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
import uuid
from typing import Any

from _tf import default_env, tf_output
from acceptance_phase2 import Ctx, in_status, settled, wait
from PIL import Image, ImageDraw, ImageFont

WORDS = [
    "ingestion",
    "pipeline",
    "staging",
    "chunk",
    "embedding",
    "vector",
    "index",
    "lease",
    "reconciler",
    "process",
    "article",
    "version",
    "manifest",
    "textract",
    "scanned",
    "page",
    "paragraph",
    "retrieval",
    "quality",
    "freshness",
    "summary",
]


def scanned_pdf(pages: int, tag: str) -> bytes:
    """Letter pages at 150 dpi with typed-looking text rendered as pixels only."""
    font = ImageFont.load_default(size=30)
    images = []
    for number in range(1, pages + 1):
        img = Image.new("L", (1275, 1650), 255)
        draw = ImageDraw.Draw(img)
        y = 120
        draw.text((120, y), f"Acceptance {tag} - page {number}", fill=0, font=font)
        y += 90
        for row in range(30):
            words = [WORDS[(number * 7 + row * 3 + i) % len(WORDS)] for i in range(9)]
            draw.text((120, y), f"Line {row + 1}: " + " ".join(words) + ".", fill=0, font=font)
            y += 44 if row % 6 != 5 else 88  # a wider gap every 6 lines: a new paragraph
        images.append(img)
    out = io.BytesIO()
    images[0].save(out, format="PDF", save_all=True, append_images=images[1:], resolution=150)
    return out.getvalue()


def uc12_ocr_off(ctx: Ctx, pages: int) -> None:
    """With ocr_enabled = false (free-plan dev): a scanned PDF is SKIPPED, not failed."""
    tag = f"UC12-off-{uuid.uuid4().hex[:6]}"
    print(f"UC12 (OCR off): {pages}-page scanned PDF is skipped")
    result = ctx.ingest_files({f"{tag.lower()}.pdf": scanned_pdf(pages, tag)}, f"acceptance {tag}")
    pid, a = result["process_id"], result["uploaded"][0]["article_id"]
    latest = wait(lambda: in_status(ctx, a, "SKIPPED") or in_status(ctx, a, "FAILED_PERMANENT")
                  or in_status(ctx, a, "INDEXED"), "UC12 outcome", timeout=600, every=5)  # fmt: skip
    ctx.check(latest.get("status") == "SKIPPED" and latest.get("skip_reason") == "E_EMPTY_TEXT",
              f"{latest.get('status')} {latest.get('skip_reason') or latest.get('error_code')}",
              "UC12")  # fmt: skip
    summary = wait(lambda: settled(ctx, pid), "UC12 process")
    ctx.check(ctx.counts(pid) == {"indexed": 0, "skipped": 1, "failed": 0},
              f"counted as skipped: {ctx.counts(pid)}", "UC12")  # fmt: skip
    ctx.check(summary.get("status") == "COMPLETE", f"process {summary.get('status')}", "UC12")


def uc12(ctx: Ctx, pages: int) -> None:
    tag = f"UC12-{uuid.uuid4().hex[:6]}"
    data = scanned_pdf(pages, tag)
    route = "Fetch -> OCR" if len(data) > 50 * 1024 * 1024 else "Extract -> OCR hand-off"
    print(f"UC12: {pages}-page scanned PDF ({len(data) / 1e6:.1f} MB, {route})")
    started = time.monotonic()
    result = ctx.ingest_files({f"{tag.lower()}.pdf": data}, f"acceptance {tag}")
    pid, a = result["process_id"], result["uploaded"][0]["article_id"]
    latest = wait(lambda: in_status(ctx, a, "INDEXED") or in_status(ctx, a, "FAILED_PERMANENT")
                  or in_status(ctx, a, "SKIPPED"), "UC12 outcome", timeout=3600, every=10)  # fmt: skip
    print(f"   finished in {time.monotonic() - started:.0f}s")
    ctx.check(latest.get("status") == "INDEXED",
              f"INDEXED ({latest.get('status')} {latest.get('error_code', '')})", "UC12")  # fmt: skip
    items = {str(i["sk"]).rsplit("_", 1)[-1]: i for i in ctx.steps(a, str(latest["run_id"]))}
    extract = items.get("EXTRACT", {})
    metrics = extract.get("metrics") or {}
    ctx.check(metrics.get("extractor") == "textract", f"extracted by {metrics.get('extractor')}",
              "UC12")  # fmt: skip
    ctx.check(int(metrics.get("pages", 0)) == pages, f"pages {metrics.get('pages')}", "UC12")
    ctx.check(int(extract.get("attempt", 0)) == 1, f"Extract attempt {extract.get('attempt')}",
              "UC12")  # fmt: skip
    ctx.check(len(items) + 2 == 10, f"RUN# + {len(items)} steps + LATEST = 10", "UC12")
    ctx.check(int(latest.get("chunk_count", 0)) >= pages,
              f"chunks {latest.get('chunk_count')} for {pages} pages", "UC12")  # fmt: skip
    summary = wait(lambda: settled(ctx, pid), "UC12 process")
    ctx.check(summary.get("status") == "COMPLETE", f"process {summary.get('status')}", "UC12")


# -- UC2, UC9, UC10 --------------------------------------------------------------------------


def tables(ctx: Ctx) -> dict[str, Any]:
    # With the account's 10-Lambda concurrency limit, a busy pipeline can throttle the control
    # API (API Gateway answers 500 without invoking it): retry those.
    status, out = 0, None
    for attempt in range(8):
        status, out = ctx.control.call("GET", "/v1/tables")
        if status == 200:
            return dict(out)
        if status < 500:
            break
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"GET /v1/tables -> {status} {out}")


def reindex(ctx: Ctx, body: dict[str, Any]) -> None:
    status, out = ctx.control.call("POST", "/v1/reindex", body)
    if status != 202:
        raise RuntimeError(f"POST /v1/reindex -> {status} {out}")
    arn = str(out["execution_arn"])
    print(f"   reindex {body} -> {out['count']} article(s)")

    def finished() -> dict[str, Any] | None:
        d = ctx.env.sfn.describe_execution(executionArn=arn)
        return dict(d) if d["status"] != "RUNNING" else None

    final = wait(finished, "reindex execution", timeout=900, every=5)
    if final["status"] != "SUCCEEDED":
        raise RuntimeError(f"reindex execution {final['status']}")


def table_reaches(ctx: Ctx, table: str, articles: int) -> dict[str, Any] | None:
    t = tables(ctx)
    return t if t["tables"][table]["active_articles"] >= articles else None


def uc9(ctx: Ctx) -> None:
    print("UC9: model change -> new table filled from staged chunks; view switched; rollback works")
    new, live = "chunks_compact", "chunks_primary"
    before = tables(ctx)
    ctx.check(before["active_table"] == live, f"live table {before['active_table']}", "UC9")
    expected = before["tables"][live]["active_articles"]
    reindex(ctx, {"target_table": new, "from_stage": "EMBED"})
    filled = wait(lambda: table_reaches(ctx, new, expected), "the new table", 1200, 15)
    got = filled["tables"][new]["active_articles"]
    ctx.check(got == expected, f"{new}: {got} of {expected} articles", "UC9")
    ctx.check(filled["tables"][live] == before["tables"][live], "live table untouched", "UC9")

    status, _ = ctx.control.call("POST", "/v1/tables/active", {"table": new})
    ctx.check(status == 200 and tables(ctx)["active_table"] == new, f"switched to {new}", "UC9")
    status, _ = ctx.control.call("POST", "/v1/tables/active", {"table": live})
    after = tables(ctx)
    ctx.check(status == 200 and after["active_table"] == live, "rolled back", "UC9")
    ctx.check(after["tables"][live] == before["tables"][live], "rollback lost nothing", "UC9")


def uc10(ctx: Ctx) -> None:
    print("UC10: rebuild -> truncate, from_stage=INDEX restores row counts, zero Bedrock calls")
    live = "chunks_primary"
    before = tables(ctx)["tables"][live]
    since = time.time()
    payload = json.dumps({"action": "truncate", "table": live, "confirm": live}).encode()
    migrate = tf_output("ingestion", ctx.env.env, "migrate_function_name")
    resp = ctx.lam.invoke(FunctionName=migrate, Payload=payload)
    ctx.check(not resp.get("FunctionError"), "truncated", "UC10")
    ctx.check(tables(ctx)["tables"][live]["rows"] == 0, "table empty", "UC10")
    reindex(ctx, {"from_stage": "INDEX"})
    restored = wait(
        lambda: table_reaches(ctx, live, before["active_articles"]), "rebuild", 1200, 15
    )
    got = restored["tables"][live]
    ctx.check(got == before, f"row counts restored: {got} (before {before})", "UC10")

    starts = set()
    executions = ctx.env.sfn.list_executions(stateMachineArn=ctx.env.state_machine, maxResults=100)
    for e in executions["executions"]:
        if e["startDate"].timestamp() < since:
            break
        article = e["name"].split("-", 1)[0]
        run_id = str(ctx.latest(article).get("run_id"))
        keys = sorted(str(i["sk"]) for i in ctx.steps(article, run_id))  # numbered: in order
        starts.add(keys[0].rsplit("_", 1)[-1] if keys else "-")
    ctx.check(starts == {"INDEX"}, f"rebuild runs start at Index (no Embed): {starts}", "UC10")


def newer_indexed(ctx: Ctx, article_id: str, version: int) -> dict[str, Any] | None:
    latest = ctx.latest(article_id)
    done = latest.get("status") == "INDEXED" and int(latest.get("doc_version", 0)) > version
    return latest if done else None


def uc2(ctx: Ctx) -> None:
    print("UC2: changed content -> doc_version 2, cache hits for unchanged chunks")
    filename = f"uc2-{uuid.uuid4().hex[:6]}.txt"
    paragraphs = [
        f"Section {i}. " + " ".join(f"Detail {i}.{j} about the second use case." for j in range(40))
        for i in range(6)
    ]
    first = ctx.ingest_files({filename: "\n\n".join(paragraphs).encode()}, "acceptance UC2 v1")
    a = first["uploaded"][0]["article_id"]
    v1 = wait(lambda: in_status(ctx, a, "INDEXED"), "UC2 v1", timeout=300)
    paragraphs[-1] = "Section 5 rewritten. " + " ".join(f"New fact {j}." for j in range(60))
    ctx.ingest_files({filename: "\n\n".join(paragraphs).encode()}, "acceptance UC2 v2")
    latest = wait(lambda: newer_indexed(ctx, a, int(v1["doc_version"])), "UC2 v2", timeout=300)
    ctx.check(int(latest["doc_version"]) == int(v1["doc_version"]) + 1,
              f"doc_version {v1['doc_version']} -> {latest['doc_version']}", "UC2")  # fmt: skip
    items = {str(i["sk"]).rsplit("_", 1)[-1]: i for i in ctx.steps(a, str(latest["run_id"]))}
    m = items.get("EMBED", {}).get("metrics") or {}
    hits, calls = int(m.get("cache_hits", 0)), int(m.get("bedrock_calls", 0))
    ctx.check(hits > 0 and calls > 0, f"cache hits {hits}, Bedrock calls {calls}", "UC2")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", choices=["uc12", "uc2", "uc9", "uc10"])
    parser.add_argument("--pages", type=int, default=3)
    parser.add_argument("--env", default=default_env())
    parser.add_argument(
        "--ocr-off", action="store_true", help="expect SKIPPED (ocr_enabled = false)"
    )
    args = parser.parse_args()
    ctx = Ctx(args.env)
    if args.scenario == "uc12":
        (uc12_ocr_off if args.ocr_off else uc12)(ctx, args.pages)
    else:
        {"uc2": uc2, "uc9": uc9, "uc10": uc10}[args.scenario](ctx)
    if ctx.failures:
        print("FAILED\n  " + "\n  ".join(ctx.failures))
        return 1
    print("PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
