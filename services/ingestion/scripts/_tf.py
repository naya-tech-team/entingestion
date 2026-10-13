"""Terraform outputs for the operator scripts: every deployed name (functions, tables, queues,
URLs) comes from a stack's outputs, never from a naming convention.

    tf_output("ingestion", "dev", "status_table_name")   one output, as text
    tf_json("ingestion", "dev", "function_names")        one output, decoded (maps, lists)
    function_name("dev", "dispatcher")                   an ingestion function by short name
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]


def _output(stack: str, env: str, *args: str) -> str:
    return subprocess.check_output(
        ["terraform", "output", *args],
        cwd=ROOT / "terraform" / "stacks" / stack,
        env={**os.environ, "TF_DATA_DIR": f".terraform/{env}"},
        text=True,
    ).strip()


def tf_output(stack: str, env: str, name: str) -> str:
    """A string output of a stack in an environment."""
    return _output(stack, env, "-raw", name)


def tf_json(stack: str, env: str, name: str) -> Any:
    """Any output (maps, lists) of a stack in an environment."""
    return json.loads(_output(stack, env, "-json", name))


def function_name(env: str, short: str) -> str:
    """An ingestion function's full name (output function_names: short name -> name)."""
    names: dict[str, str] = tf_json("ingestion", env, "function_names")
    if short not in names:
        raise KeyError(f"no ingestion function {short!r}; known: {sorted(names)}")
    return names[short]


def default_env() -> str:
    """The environment scripts act on unless --env says otherwise: $ENV, else dev."""
    return os.environ.get("ENV") or "dev"
