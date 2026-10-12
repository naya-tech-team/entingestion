"""Each Lambda imports only what its package (core or docs) ships.

The function -> package map is read from the Terraform stack, and the modules each package lacks
are worked out from installed metadata, so this stays in sync with functions.tf and pyproject.toml.
A failure here is a Runtime.ImportModuleError in AWS.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

FUNCTIONS_TF = Path(__file__).resolve().parents[4] / "terraform/stacks/ingestion/functions.tf"
GROUPS = ("core", "docs")

_FUNCTION = re.compile(
    r'^\s*(\w+)\s*=\s*\{\s*handler\s*=\s*"([\w.]+)",\s*package\s*=\s*"(\w+)"', re.M
)


def _handlers() -> list[tuple[str, str, str]]:
    found = [
        (name, handler.rsplit(".", 1)[0], group)
        for name, handler, group in _FUNCTION.findall(FUNCTIONS_TF.read_text())
    ]
    assert found, f"no functions found in {FUNCTIONS_TF}"
    return found


def _closure(extra: str) -> set[str]:
    """Distributions installed for `ingest[extra]`, including transitive dependencies."""
    seen: set[str] = set()
    todo = [(r, extra) for r in metadata.requires("ingest") or []]
    while todo:
        raw, active = todo.pop()
        req = Requirement(raw)
        if req.marker and not req.marker.evaluate({"extra": active}):
            continue
        name = canonicalize_name(req.name)
        if name in seen:
            continue
        seen.add(name)
        children = metadata.requires(req.name) or []
        todo += [(c, "") for c in children]
        todo += [(c, e) for e in req.extras for c in children]
    return seen


def _missing_modules(group: str) -> list[str]:
    """Top-level modules that only the other package provides."""
    own = _closure(group)
    other = set().union(*(_closure(g) for g in GROUPS if g != group)) - own
    return sorted(
        module
        for module, dists in metadata.packages_distributions().items()
        if {canonicalize_name(d) for d in dists} <= other
    )


_PROBE = """
import importlib, importlib.abc, json, sys
blocked = set(json.loads(sys.argv[2]))
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in blocked:
            raise ModuleNotFoundError(f"No module named {name!r} (not in this package)")
        return None
sys.meta_path.insert(0, Block())
importlib.import_module(sys.argv[1])
"""


@pytest.mark.parametrize(("function", "module", "group"), _handlers())
def test_handler_imports_with_its_package_only(function: str, module: str, group: str) -> None:
    assert group in GROUPS, f"{function}: unknown package {group}"
    blocked = _missing_modules(group)
    run = subprocess.run(  # noqa: S603 - fixed argv built by this test
        [sys.executable, "-c", _PROBE, module, json.dumps(blocked)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, f"{function} ({group}) can't import {module}:\n{run.stderr[-2000:]}"
