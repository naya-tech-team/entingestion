"""PII screening in Normalize, when PII_SCREENING is on (off by default: Comprehend
is paid and not on the AWS free plan).

Amazon Comprehend DetectPiiEntities runs over the title and every block, packed into requests
under its 100 KB limit; entity offsets are mapped back to their text. Entities scoring below
PII_MIN_SCORE are ignored. What happens next is the source's policy (the producer's source access
policy, "pii" per source; ingest.access):

    allow   index as is (the default); what was found is recorded
    redact  replace each entity with [TYPE], e.g. [EMAIL], before chunking and embedding
    block   don't index the article: SKIPPED with E_PII_BLOCKED

The finding (types and counts, the action) goes on the normalized document and LATEST.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ingest.access import PiiPolicy

MAX_BYTES = 90_000  # per request; Comprehend's limit is 100 KB of UTF-8
_SEPARATOR = "\n\n"
LANGUAGE = "en"

Detect = Callable[[str], list[dict[str, Any]]]


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    type: str


@dataclass
class Screening:
    spans: list[list[Span]]  # per input text
    counts: Counter[str] = field(default_factory=Counter)

    @property
    def found(self) -> int:
        return sum(self.counts.values())

    def summary(self, action: PiiPolicy) -> dict[str, Any]:
        return {"count": self.found, "types": dict(sorted(self.counts.items())), "action": action}


def comprehend_detector(client: Any) -> Detect:
    def detect(text: str) -> list[dict[str, Any]]:
        resp = client.detect_pii_entities(Text=text, LanguageCode=LANGUAGE)
        return list(resp.get("Entities") or [])

    return detect


def _pieces(texts: list[str]) -> list[tuple[int, int, str]]:
    """(text index, offset in it, piece): texts cut so each piece fits a request."""
    out = []
    for i, text in enumerate(texts):
        at = 0
        while at < len(text):
            piece = text[at : at + MAX_BYTES // 4]  # 4 bytes per character at most
            while len(piece.encode()) > MAX_BYTES:
                piece = piece[: len(piece) // 2]
            out.append((i, at, piece))
            at += len(piece)
    return out


def screen(texts: list[str], detect: Detect, min_score: float) -> Screening:
    """Entities per text, from as few requests as fit."""
    result = Screening([[] for _ in texts])
    batch: list[tuple[int, int, str, int]] = []  # (text, offset in text, piece, offset in request)
    size = 0

    def flush() -> None:
        nonlocal batch, size
        if not batch:
            return
        request = _SEPARATOR.join(piece for _, _, piece, _ in batch)
        for entity in detect(request):
            if float(entity.get("Score", 0)) < min_score:
                continue
            begin, end = int(entity["BeginOffset"]), int(entity["EndOffset"])
            for index, offset, piece, at in batch:
                if at <= begin < at + len(piece):
                    stop = min(end, at + len(piece))  # never into the separator or next piece
                    span = Span(offset + begin - at, offset + stop - at, str(entity["Type"]))
                    result.spans[index].append(span)
                    result.counts[span.type] += 1
                    break
        batch, size = [], 0

    for index, offset, piece in _pieces(texts):
        n = len(piece.encode()) + len(_SEPARATOR)
        if batch and size + n > MAX_BYTES:
            flush()
        at = sum(len(p) + len(_SEPARATOR) for _, _, p, _ in batch)
        batch.append((index, offset, piece, at))
        size += n
    flush()
    return result


def redact(text: str, spans: list[Span]) -> str:
    """Each span replaced by [TYPE]; overlapping spans count once."""
    out, at = [], 0
    for span in sorted(spans, key=lambda s: (s.start, -s.end)):
        if span.start < at:
            continue
        out += [text[at : span.start], f"[{span.type}]"]
        at = span.end
    out.append(text[at:])
    return "".join(out)
