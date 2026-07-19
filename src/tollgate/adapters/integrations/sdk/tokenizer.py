"""Pluggable input tokenizer for the reserve input bound.

The core ships a conservative heuristic so the SDK has no hard native dependency; a
``tiktoken``-backed tokenizer is available when the ``tokenizers`` extra is installed. Both feed
``input_bound_tokens``, which adds a fixed provider margin covering stable provider-side overhead
so the bound stays in the safe over-reserve direction (section 4).
"""

from __future__ import annotations

import math
from typing import Protocol, runtime_checkable


@runtime_checkable
class Tokenizer(Protocol):
    """Counts the tokens a prompt will cost as input."""

    def count_tokens(self, text: str) -> int: ...


class HeuristicTokenizer:
    """A dependency-free upper-bound estimate: ``ceil(len(text) / 3)``.

    Intentionally coarse and high — over-counting reserves more, which is the safe direction; a
    caller that wants a tight bound installs the ``tokenizers`` extra (see :func:`try_tiktoken`).
    """

    def count_tokens(self, text: str) -> int:
        return math.ceil(len(text) / 3)


def input_bound_tokens(tokenizer: Tokenizer, prompt: str, *, provider_margin_tokens: int) -> int:
    """Tokenizer count plus a fixed provider margin — the worst-case input bound for reserve."""
    return tokenizer.count_tokens(prompt) + provider_margin_tokens


def try_tiktoken(model: str) -> Tokenizer | None:
    """A ``tiktoken`` tokenizer for ``model`` if the extra is installed, else ``None``.

    Never raises on absence: an ``ImportError`` (extra not installed) or an unknown model both
    return ``None`` so the caller can fall back to :class:`HeuristicTokenizer`.
    """
    try:
        import tiktoken
    except ImportError:
        return None
    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError:
        encoding = tiktoken.get_encoding("cl100k_base")

    class _Tiktoken:
        def count_tokens(self, text: str) -> int:
            return len(encoding.encode(text))

    return _Tiktoken()
