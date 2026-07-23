"""Unit tests for the harness's pure numbers-table formatter."""

from __future__ import annotations

from loadtest.harness import RunMetrics, format_table


def _row(strategy: str, overspend: int, retries: int) -> RunMetrics:
    return RunMetrics(
        strategy=strategy,
        concurrency=32,
        ops=128,
        admitted=10,
        denied=118,
        retries=retries,
        throughput_ops_per_s=1234.5,
        p99_ms=6.7,
        overspend_micro=overspend,
        violations=(),
    )


def test_table_has_a_header_and_one_row_per_metric() -> None:
    table = format_table([_row("naive", 4200, 0), _row("guarded", 0, 0)])
    lines = table.splitlines()
    assert "strategy" in lines[0] and "overspend" in lines[0]
    assert any(line.startswith("naive") for line in lines)
    assert any(line.startswith("guarded") for line in lines)


def test_table_renders_overspend_and_retries() -> None:
    table = format_table([_row("naive", 4200, 0), _row("occ", 0, 91)])
    assert "4200" in table
    assert "91" in table


def test_empty_rows_still_render_a_header() -> None:
    assert "strategy" in format_table([])
