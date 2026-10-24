"""Small hand-calculated gold cases for combined restricted analysis plans."""

from __future__ import annotations

import csv
import random
from decimal import Decimal
from pathlib import Path

import pytest

from lantai.analysis import AnalysisService


PAID = {"column": "status", "op": "eq", "value": "paid"}
CANCELLED = {"column": "status", "op": "eq", "value": "cancelled"}
AMOUNT = {"column": "amount", "op": "sum", "as": "revenue"}
MONTH = {"column": "date", "grain": "month", "as": "month"}


@pytest.mark.parametrize("name, plan, expected_rows", [
    ("source_rows", {"deduplicate_by": [], "metrics": [{"op": "count", "as": "orders"}]}, [{"orders": 7}]),
    ("deduplicated_rows", {"metrics": [{"op": "count", "as": "orders"}]}, [{"orders": 6}]),
    ("filtered_sum", {"filters": [PAID], "metrics": [AMOUNT]}, [{"revenue": "60"}]),
    ("filtered_count", {"filters": [PAID], "metrics": [{"column": "amount", "op": "count", "as": "nonmissing"}]}, [{"nonmissing": 4}]),
    ("distinct_regions", {"filters": [PAID], "metrics": [{"column": "region", "op": "count_distinct", "as": "regions"}]}, [{"regions": 2}]),
    ("numeric_filter", {"filters": [PAID, {"column": "amount", "op": "gte", "value": 20}], "metrics": [AMOUNT]}, [{"revenue": "50"}]),
    ("grouped_share_rank", {"filters": [PAID], "group_by": ["region"], "metrics": [AMOUNT], "shares": [{"metric": "revenue", "as": "share"}], "ranks": [{"metric": "revenue", "as": "rank", "descending": True, "method": "competition"}], "sort": [{"column": "region"}]}, [{"region": "East", "revenue": "30", "share": "0.5", "rank": 1}, {"region": "West", "revenue": "30", "share": "0.5", "rank": 1}]),
    ("monthly_sum", {"filters": [PAID], "date_buckets": [MONTH], "group_by": ["month"], "metrics": [AMOUNT], "sort": [{"column": "month"}]}, [{"month": "2026-01", "revenue": "30"}, {"month": "2026-02", "revenue": "30"}]),
    ("metric_specific_filters", {"metrics": [{**AMOUNT, "as": "paid", "where": [PAID]}, {**AMOUNT, "as": "cancelled", "where": [CANCELLED]}], "comparisons": [{"current": "paid", "baseline": "cancelled", "op": "difference", "as": "difference"}, {"current": "paid", "baseline": "cancelled", "op": "growth_rate", "as": "growth"}]}, [{"paid": "60", "cancelled": "30", "difference": "30", "growth": "1"}]),
    ("average", {"filters": [PAID], "metrics": [{"column": "amount", "op": "avg", "as": "average"}]}, [{"average": "15"}]),
    ("min_max", {"filters": [PAID], "metrics": [{"column": "amount", "op": "min", "as": "min"}, {"column": "amount", "op": "max", "as": "max"}]}, [{"min": "0", "max": "30"}]),
    ("ratio", {"filters": [PAID], "metrics": [AMOUNT, {"op": "count", "as": "orders"}], "ratios": [{"numerator": "revenue", "denominator": "orders", "as": "per_order"}]}, [{"revenue": "60", "orders": 5, "per_order": "12"}]),
    ("monthly_pivot", {"filters": [PAID], "date_buckets": [MONTH], "group_by": ["month", "region"], "metrics": [AMOUNT], "pivot": {"column": "region", "metric": "revenue", "as_prefix": "revenue_"}, "sort": [{"column": "month"}]}, [{"month": "2026-01", "revenue_East": "30", "revenue_West": None}, {"month": "2026-02", "revenue_East": None, "revenue_West": "30"}]),
], ids=lambda value: value if isinstance(value, str) else None)
def test_hand_calculated_combination_matrix(tmp_path: Path, name: str, plan: dict, expected_rows: list[dict]) -> None:
    source = tmp_path / "orders.csv"
    with source.open("w", newline="", encoding="utf-8") as stream:
        csv.writer(stream).writerows([
            ["id", "date", "region", "status", "amount"],
            ["1", "2026-01-01", "East", "paid", "10"],
            ["2", "2026-01-02", "East", "paid", "20"],
            ["2", "2026-01-02", "East", "paid", "20"],
            ["3", "2026-01-03", "West", "cancelled", "30"],
            ["4", "2026-02-01", "West", "paid", "30"],
            ["5", "2026-02-02", "East", "paid", ""],
            ["6", "2026-02-03", "West", "paid", "0"],
        ])
    service = AnalysisService(tmp_path / "state", scope="gold-matrix")
    dataset = service.ingest(source)
    result = service.run(dataset["dataset_id"], {"deduplicate_by": ["id"], **plan})
    assert result["rows"] == expected_rows, name
    assert result["evidence"]["source_row_count"] == 7
    assert result["evidence"]["source_sha256"] == dataset["source_sha256"]
    assert result["verification"]["source_integrity"]["status"] == "passed"
    assert result["verification"]["semantic_review"]["status"] == "pending"


@pytest.mark.parametrize("seed", range(20))
def test_seeded_grouped_decimal_oracle(tmp_path: Path, seed: int) -> None:
    """Compare 10,240 unique rows plus exact duplicates to an independent oracle."""

    rng = random.Random(seed)
    unique_rows: list[tuple[str, str, str, str]] = []
    source_rows: list[tuple[str, str, str, str]] = []
    for order_id in range(512):
        cents = rng.randrange(0, 100_000)
        amount = "" if order_id % 17 == 0 else f"{cents // 100}.{cents % 100:02d}"
        row = (str(order_id), rng.choice(("East", "West", "North", "South")), rng.choice(("paid", "cancelled")), amount)
        unique_rows.append(row)
        source_rows.append(row)
        if order_id % 7 == 0:
            source_rows.append(row)

    source = tmp_path / "orders.csv"
    with source.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("id", "region", "status", "amount"))
        writer.writerows(source_rows)

    expected: dict[str, dict[str, object]] = {}
    for _order_id, region, status, amount in unique_rows:
        if status != "paid":
            continue
        record = expected.setdefault(region, {"region": region, "orders": 0, "recorded": 0, "revenue": Decimal("0.00")})
        record["orders"] += 1
        if amount:
            record["recorded"] += 1
            record["revenue"] += Decimal(amount)

    service = AnalysisService(tmp_path / "state", scope=f"seed-{seed}")
    dataset = service.ingest(source)
    result = service.run(dataset["dataset_id"], {
        "deduplicate_by": ["id"],
        "filters": [PAID],
        "group_by": ["region"],
        "metrics": [
            {"op": "count", "as": "orders"},
            {"column": "amount", "op": "count", "as": "recorded"},
            AMOUNT,
        ],
        "sort": [{"column": "region"}],
    })
    expected_rows = [
        {**record, "revenue": format(record["revenue"], "f")}
        for _region, record in sorted(expected.items())
    ]
    assert result["rows"] == expected_rows
    assert result["evidence"]["source_row_count"] == len(source_rows)
    assert result["evidence"]["source_sha256"] == dataset["source_sha256"]
