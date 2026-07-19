"""Tests for the SDK tokenizer abstraction and the input-bound derivation."""

from __future__ import annotations

from tollgate.adapters.integrations.sdk.tokenizer import (
    HeuristicTokenizer,
    input_bound_tokens,
    try_tiktoken,
)


def test_heuristic_over_counts_in_the_safe_direction() -> None:
    # ceil(len/3) is a deliberate over-count vs. the ~4 chars/token rule of thumb.
    assert HeuristicTokenizer().count_tokens("") == 0
    assert HeuristicTokenizer().count_tokens("abc") == 1
    assert HeuristicTokenizer().count_tokens("abcd") == 2


def test_input_bound_adds_the_provider_margin() -> None:
    tok = HeuristicTokenizer()
    # ceil(6/3) = 2, plus a margin of 5.
    assert input_bound_tokens(tok, "abcdef", provider_margin_tokens=5) == 7


def test_try_tiktoken_returns_none_or_a_tokenizer_without_raising() -> None:
    # Absent extra -> None; present -> a working tokenizer. Never raises on absence.
    tok = try_tiktoken("gpt-4o")
    assert tok is None or tok.count_tokens("hello world") >= 1
