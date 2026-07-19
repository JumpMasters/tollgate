"""The SDK's public surface is importable from the package root."""

from __future__ import annotations


def test_public_symbols_are_exported() -> None:
    from tollgate.adapters.integrations import sdk

    for name in (
        "guard",
        "TollgateClient",
        "SdkConfig",
        "Tokenizer",
        "HeuristicTokenizer",
        "ProviderUsage",
        "EnforcementUnavailable",
        "BudgetDenied",
    ):
        assert hasattr(sdk, name), name
