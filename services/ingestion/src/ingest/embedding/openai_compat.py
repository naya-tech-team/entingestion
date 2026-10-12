"""Any server that speaks the OpenAI embeddings API, over plain HTTPS (urllib; no SDK): a LiteLLM
proxy, vLLM, Hugging Face TEI, Ollama, ...

    POST {base_url}/embeddings
    Authorization: Bearer <key>          (the profile's api_key_secret, from Secrets Manager)
    {"model": ..., "input": [<= batch_size texts],
     "dimensions": <dimension>           (send_dimensions: Matryoshka models)
     "input_type": ...}                  (document/query_input_type; LiteLLM passes it on, e.g.
                                         to Cohere)
    -> {"data": [{"index": 0, "embedding": [...]}, ...], "usage": {"prompt_tokens": N}}

Errors map to the pipeline's codes: 429 -> E_THROTTLED; 5xx, connection errors and timeouts ->
E_UPSTREAM_5XX / E_TIMEOUT (retried); any other 4xx -> E_UPSTREAM_4XX (permanent: a bad model
name, key or request won't fix itself). Each call is retried a few times here first, like the
AWS clients do.

Vectors are L2-normalized here when the profile says normalize (servers differ in whether they
do), so Validate's unit-length check holds for every provider.
"""

from __future__ import annotations

import json
import random
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urlsplit

from ingest.embedding.base import Embedding, l2_normalize, spread_tokens
from ingest.embedding.profiles import LOCAL_HOSTS, EmbeddingProfile
from ingest.errors import ErrorCode, IngestError, make_error

TIMEOUT_SECONDS = 60
ATTEMPTS = 3
BACKOFF_SECONDS = 1.0
MAX_RETRY_AFTER = 20.0
_HTTP_TOO_MANY = 429
_HTTP_SERVER = 500

# Per container: Secrets Manager is read once per secret, not per call.
_keys: dict[str, str] = {}
_keys_lock = threading.Lock()


def api_key(secret_name: str, fetch: Callable[[str], str]) -> str:
    """The API key in a secret: its SecretString, or the "api_key" field of a JSON one."""
    with _keys_lock:
        if secret_name not in _keys:
            raw = fetch(secret_name).strip()
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = raw
            key = parsed.get("api_key") if isinstance(parsed, dict) else parsed
            if not isinstance(key, str) or not key:
                raise make_error(ErrorCode.BUG, f"secret {secret_name} holds no API key")
            _keys[secret_name] = key
        return _keys[secret_name]


def reset_keys() -> None:
    """For tests: forget cached API keys."""
    with _keys_lock:
        _keys.clear()


class OpenAICompatibleEmbedder:
    def __init__(
        self,
        profile: EmbeddingProfile,
        key: str | None = None,
        *,
        timeout: float = TIMEOUT_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not profile.base_url:
            raise make_error(ErrorCode.BUG, f"profile {profile.name} has no base_url")
        url = urlsplit(profile.base_url)
        if url.scheme != "https" and not (url.scheme == "http" and url.hostname in LOCAL_HOSTS):
            raise make_error(ErrorCode.BUG, f"profile {profile.name}: base_url must be https")
        self.profile = profile
        self.url = f"{profile.base_url.rstrip('/')}/embeddings"
        self.key = key
        self.timeout = timeout
        self.sleep = sleep
        self.batch_size = profile.effective_batch_size

    def embed_documents(self, texts: Sequence[str]) -> list[Embedding]:
        return self._embed(texts, self.profile.input_type("document"))

    def embed_query(self, text: str) -> Embedding:
        return self._embed([text], self.profile.input_type("query"))[0]

    def _embed(self, texts: Sequence[str], input_type: str | None) -> list[Embedding]:
        body: dict[str, Any] = {"model": self.profile.model, "input": list(texts)}
        if self.profile.send_dimensions:
            body["dimensions"] = self.profile.dimension
        if input_type:
            body["input_type"] = input_type
        payload = self._post(body)
        data = payload.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            got = len(data) if isinstance(data, list) else type(data).__name__
            raise make_error(
                ErrorCode.UPSTREAM_5XX, f"{self.url}: {got} embeddings for {len(texts)} inputs"
            )
        ordered = sorted(data, key=lambda d: int(d.get("index", 0))) if _indexed(data) else data
        usage = payload.get("usage") or {}
        tokens = spread_tokens(int(usage.get("prompt_tokens") or 0), texts)
        out = []
        for item, n in zip(ordered, tokens, strict=True):
            values = [float(x) for x in item["embedding"]]
            out.append(Embedding(l2_normalize(values) if self.profile.normalize else values, n))
        return out

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(1, ATTEMPTS + 1):
            try:
                return self._post_once(body)
            except _Retry as retry:
                if attempt == ATTEMPTS:
                    raise retry.error from None
                delay = retry.after or BACKOFF_SECONDS * 2 ** (attempt - 1)
                self.sleep(min(delay, MAX_RETRY_AFTER) * (0.5 + random.random() / 2))  # noqa: S311
        raise AssertionError("unreachable")  # pragma: no cover

    def _post_once(self, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        request = urllib.request.Request(  # noqa: S310 - scheme checked in __init__
            self.url, data=json.dumps(body).encode(), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:  # noqa: S310
                return dict(json.loads(resp.read()))
        except urllib.error.HTTPError as err:
            raise _http_error(self.url, err) from None
        except TimeoutError as err:
            raise _Retry(make_error(ErrorCode.TIMEOUT, f"{self.url}: {err}")) from None
        except (urllib.error.URLError, ConnectionError) as err:
            raise _Retry(make_error(ErrorCode.UPSTREAM_5XX, f"{self.url}: {err}")) from None
        except ValueError as err:  # not JSON
            raise _Retry(make_error(ErrorCode.UPSTREAM_5XX, f"{self.url}: {err}")) from None


class _Retry(Exception):
    def __init__(self, error: IngestError, after: float | None = None) -> None:
        super().__init__(str(error))
        self.error = error
        self.after = after


def _indexed(data: list[Any]) -> bool:
    return all(isinstance(d, dict) and "index" in d for d in data)


def _http_error(url: str, err: urllib.error.HTTPError) -> Exception:
    try:
        detail = err.read().decode(errors="replace")[:300]
    except Exception:
        detail = ""
    message = f"{url}: HTTP {err.code} {detail}".strip()
    if err.code == _HTTP_TOO_MANY:
        after = err.headers.get("Retry-After") if err.headers else None
        try:
            wait = float(after) if after else None
        except ValueError:
            wait = None
        return _Retry(make_error(ErrorCode.THROTTLED, message), wait)
    if err.code >= _HTTP_SERVER:
        return _Retry(make_error(ErrorCode.UPSTREAM_5XX, message))
    return make_error(ErrorCode.UPSTREAM_4XX, message)
