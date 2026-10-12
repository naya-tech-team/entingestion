"""Write a deterministic sample corpus for the Phase 1 gate: N articles as HTML, Markdown and text.

uv run python scripts/sample_corpus.py <out_dir> [--count 1000] [--seed 7]
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

TOPICS = [
    "solar storage",
    "river ecology",
    "urban transit",
    "soil health",
    "battery recycling",
    "coastal erosion",
    "crop rotation",
    "heat pumps",
    "wildfire smoke",
    "groundwater",
    "wind turbines",
    "carbon capture",
    "forest carbon",
    "air quality",
    "water reuse",
]
VERBS = ["reduces", "improves", "complicates", "reshapes", "accelerates", "stabilizes", "exposes"]
OBJECTS = [
    "regional budgets",
    "maintenance schedules",
    "public health outcomes",
    "supply chains",
    "grid reliability",
    "local employment",
    "insurance costs",
    "planning timelines",
]


def sentence(rng: random.Random, topic: str) -> str:
    return (
        f"{rng.choice(['Recent work on', 'Field data about', 'A new study of', 'Experience with'])} "
        f"{topic} {rng.choice(VERBS)} {rng.choice(OBJECTS)} by {rng.randint(3, 48)} percent "
        f"in {rng.randint(2, 19)} of the sites surveyed."
    )


def sections(rng: random.Random, topic: str) -> list[tuple[str, list[str]]]:
    out = []
    for s in range(rng.randint(1, 6)):
        paragraphs = [
            " ".join(sentence(rng, topic) for _ in range(rng.randint(2, 9)))
            for _ in range(rng.randint(1, 5))
        ]
        out.append((f"{topic.title()} part {s + 1}", paragraphs))
    return out


def html(title: str, body: list[tuple[str, list[str]]]) -> str:
    parts = [f"<h2>{h}</h2>" + "".join(f"<p>{p}</p>" for p in ps) for h, ps in body]
    return f"<!doctype html><html><head><title>{title}</title></head><body><nav>Home | About</nav><article><h1>{title}</h1>{''.join(parts)}</article><footer>Copyright</footer></body></html>"


def markdown(title: str, body: list[tuple[str, list[str]]]) -> str:
    return f"# {title}\n\n" + "\n\n".join(f"## {h}\n\n" + "\n\n".join(ps) for h, ps in body) + "\n"


def text(title: str, body: list[tuple[str, list[str]]]) -> str:
    return f"{title}\n\n" + "\n\n".join(p for _, ps in body for p in ps) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("out", type=Path)
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    rng = random.Random(args.seed)
    args.out.mkdir(parents=True, exist_ok=True)
    kinds = [("html", html)] * 6 + [("md", markdown)] * 3 + [("txt", text)] * 2
    for i in range(args.count):
        topic = rng.choice(TOPICS)
        title = f"{topic.title()} report {i:04d}"
        ext, render = rng.choice(kinds)
        folder = args.out / f"batch{i // 100:02d}"
        folder.mkdir(exist_ok=True)
        (folder / f"article-{i:04d}.{ext}").write_text(render(title, sections(rng, topic)))
    print(f"wrote {args.count} articles to {args.out}")


if __name__ == "__main__":
    main()
