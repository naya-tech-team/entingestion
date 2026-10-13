"""Runtime configuration: every environment variable the BFF reads, in one place.

The two API URLs are required; the handler builds the settings on its first (cold) invocation,
so a function deployed without them fails at once instead of on some later route.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache

# The UI's caps on the Operations actions, beyond the control API's own validation.
DEFAULT_MAX_RERUN_IDS = 100
DEFAULT_MAX_REDRIVE = 200
# CloudFront gives the origin 30 s; Lambda is stopped at its own timeout just under that.
DEFAULT_CONNECT_TIMEOUT = 3.0
DEFAULT_READ_TIMEOUT = 25.0

REQUIRED = ("PRODUCER_API_URL", "CONTROL_API_URL")


class MissingSetting(RuntimeError):
    """A required environment variable is not set."""


@dataclass(frozen=True)
class Settings:
    producer_api_url: str  # the producer API's stage invoke URL
    control_api_url: str  # the ingestion control API's stage invoke URL
    max_rerun_ids: int = DEFAULT_MAX_RERUN_IDS
    max_redrive: int = DEFAULT_MAX_REDRIVE
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    read_timeout: float = DEFAULT_READ_TIMEOUT

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        missing = [name for name in REQUIRED if not env.get(name)]
        if missing:
            raise MissingSetting(f"not set: {', '.join(missing)}")
        return cls(
            producer_api_url=env["PRODUCER_API_URL"],
            control_api_url=env["CONTROL_API_URL"],
            max_rerun_ids=int(env.get("MAX_RERUN_IDS", DEFAULT_MAX_RERUN_IDS)),
            max_redrive=int(env.get("MAX_REDRIVE", DEFAULT_MAX_REDRIVE)),
            connect_timeout=float(env.get("CONNECT_TIMEOUT", DEFAULT_CONNECT_TIMEOUT)),
            read_timeout=float(env.get("READ_TIMEOUT", DEFAULT_READ_TIMEOUT)),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
