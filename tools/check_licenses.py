"""License gate: every installed Python package must be open source under an approved license.

Policy (docs: README "Dependencies and licenses"):
- Allowed: permissive (MIT, BSD, Apache-2.0, ISC, PSF, CNRI-Python, Zlib, Unlicense, CC0,
  HPND/MIT-CMU)
  and weak copyleft used unmodified as a library (LGPL, MPL).
- Denied: strong or network copyleft (GPL, AGPL), source-available (SSPL, BUSL, Commons
  Clause) and commercial/proprietary licenses.
- A package with several licenses (an SPDX "A OR B" expression, or several license classifiers)
  passes if any of them is allowed: we can take it under that one.
- Unknown licenses fail until reviewed and added to REVIEWED below.

Run inside a service's environment:  uv run python ../../tools/check_licenses.py
"""

from __future__ import annotations

import re
import sys
from importlib.metadata import Distribution, distributions

ALLOWED = (
    "MIT",
    "BSD",
    "APACHE",
    "ISC",
    "PSF",
    "PYTHON SOFTWARE FOUNDATION",
    "ZLIB",
    "UNLICENSE",
    "CC0",
    "HPND",
    "HISTORICAL PERMISSION NOTICE",
    "CNRI-PYTHON",  # permissive, OSI-approved (early CPython license)
    "MPL",
    "MOZILLA PUBLIC LICENSE",
    "LGPL",
    "LESSER GENERAL PUBLIC LICENSE",
)
DENIED = ("AGPL", "AFFERO", "SSPL", "BUSL", "BUSINESS SOURCE", "COMMONS CLAUSE", "COMMERCIAL", "PROPRIETARY")

# Our own projects (not distributed as dependencies).
OWN = {"entingestion-common", "ingest", "producer", "ui-bff"}

# Packages whose metadata doesn't state a license, reviewed by hand: name -> license.
REVIEWED: dict[str, str] = {}


def _allowed(text: str) -> bool:
    upper = text.upper()
    if any(d in upper for d in DENIED):
        return False
    if re.search(r"(?<!L)GPL|(?<!LESSER )GENERAL PUBLIC LICENSE", upper):
        return False  # strong copyleft (GPL), as opposed to LGPL
    return any(a in upper for a in ALLOWED)


def license_of(dist: Distribution) -> tuple[list[str], str]:
    """The license alternatives a package offers, and where they came from."""
    meta = dist.metadata
    expression = (meta.get("License-Expression") or "").strip()
    if expression:
        return [alt.strip(" ()") for alt in re.split(r"\s+OR\s+", expression)], "SPDX"
    classifiers = [
        c.split("::")[-1].strip()
        for c in meta.get_all("Classifier") or []
        if c.startswith("License ::") and "OSI Approved" != c.split("::")[-1].strip()
    ]
    if classifiers:
        return classifiers, "classifier"
    field = (meta.get("License") or "").strip().splitlines()
    if field and len(field[0]) <= 120 and field[0].upper() not in ("UNKNOWN", ""):
        return [field[0]], "License field"
    return [], "none"


def main() -> int:
    failures = []
    checked = 0
    for dist in sorted(distributions(), key=lambda d: d.metadata["Name"].lower()):
        name = dist.metadata["Name"].lower()
        if name in OWN:
            continue
        checked += 1
        if name in REVIEWED:
            continue
        alternatives, source = license_of(dist)
        if not alternatives:
            failures.append(f"{name}: no license metadata (review it and add to REVIEWED)")
        elif not any(all(_allowed(part) for part in re.split(r"\s+AND\s+", alt)) for alt in alternatives):
            failures.append(f"{name}: {' OR '.join(alternatives)} ({source})")
    if failures:
        print("License check FAILED:\n  " + "\n  ".join(failures))
        return 1
    print(f"License check passed: {checked} packages, all open source under approved licenses.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
