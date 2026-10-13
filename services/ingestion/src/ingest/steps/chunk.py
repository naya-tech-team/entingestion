"""Step 5: Chunk -> chunks/chunks.jsonl."""

from __future__ import annotations

from ingest.chunking.chunker import Chunker, ChunkerConfig
from ingest.chunking.tokenizer import Tokenizer
from ingest.errors import ErrorCode, make_error
from ingest.ids import STAGING_FILES
from ingest.models.staging import NormalizedDocument
from ingest.models.status import StepName
from ingest.steps.protocol import StepContext, StepOutput, step

INPUT = STAGING_FILES["NORMALIZE"]
OUTPUT = STAGING_FILES["CHUNK"]


@step(StepName.CHUNK, input_name=INPUT)
def handler(ctx: StepContext) -> StepOutput:
    doc = NormalizedDocument.model_validate_json(ctx.staging.read(ctx.staging_prefix, INPUT))
    s = ctx.settings
    chunker = Chunker(
        Tokenizer(s.chunking.tokenizer_encoding),
        ChunkerConfig(
            target=s.chunking.target_tokens,
            overlap=s.chunking.overlap_tokens,
            min_tokens=s.chunking.min_tokens,
            max_tokens=s.chunking.max_chunk_tokens,
            parent_child=s.chunking.parent_child,
            parent_target=s.chunking.parent_target_tokens,
            child_target=s.chunking.child_target_tokens,
            child_min=s.chunking.child_min_tokens,
        ),
    )
    chunks = chunker.chunk(doc)
    if len(chunks) > s.chunking.max_chunks_per_article:
        # Bounds what one article can cost in Bedrock calls and index rows. Permanent: the
        # same document chunks the same way; raise the limit and replay to index it.
        raise make_error(
            ErrorCode.TOO_LARGE,
            f"{len(chunks)} chunks, more than max_chunks_per_article"
            f" ({s.chunking.max_chunks_per_article})",
        )
    body = "\n".join(c.model_dump_json() for c in chunks).encode()
    staged = ctx.staging.write(ctx.staging_prefix, OUTPUT, body, ctx.run_id, "application/x-ndjson")
    tokens = [c.token_count for c in chunks]
    return StepOutput(
        output_uri=staged.uri,
        output_etag=staged.etag,
        metrics={
            "chunks": len(chunks),
            "tokens_total": sum(tokens),
            "tokens_max": max(tokens, default=0),
        },
        result={"chunk_count": len(chunks)},
    )
