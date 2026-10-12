"""Token counting. Titan's tokenizer isn't published, so cl100k_base approximates it; Embed
records Bedrock's own inputTextTokenCount and Validate checks the model limit with it.

The encoding file is bundled in the Lambda zip (TIKTOKEN_CACHE_DIR): there's no internet access.
"""

from __future__ import annotations

from functools import lru_cache

import tiktoken


@lru_cache(maxsize=4)
def encoding(name: str) -> tiktoken.Encoding:
    return tiktoken.get_encoding(name)


class Tokenizer:
    def __init__(self, encoding_name: str = "cl100k_base") -> None:
        self._enc = encoding(encoding_name)

    def count(self, text: str) -> int:
        return len(self._enc.encode(text, disallowed_special=()))

    def tail(self, text: str, tokens: int) -> str:
        """The last `tokens` tokens of text (for overlap)."""
        ids = self._enc.encode(text, disallowed_special=())
        return self._enc.decode(ids[-tokens:]) if tokens > 0 else ""

    def split(self, text: str, max_tokens: int) -> list[str]:
        """Hard split by token count (last resort for oversized atomic blocks)."""
        ids = self._enc.encode(text, disallowed_special=())
        return [self._enc.decode(ids[i : i + max_tokens]) for i in range(0, len(ids), max_tokens)]
