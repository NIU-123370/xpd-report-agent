from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from xpd_report_agent.hermes_plugin.db_query.data_quality import analyze_query_quality


def test_quality_analyzer_calculates_freshness_coverage_and_risks():
    quality = analyze_query_quality(
        columns=["stat_date", "item_id", "exposure_denominator", "order_count"],
        rows=[
            {
                "stat_date": "2026-08-01",
                "item_id": "A",
                "exposure_denominator": 0,
                "order_count": 5,
            },
            {
                "stat_date": "2026-08-02",
                "item_id": "B",
                "exposure_denominator": 100,
                "order_count": 40,
            },
            {
                "stat_date": "2026-08-03",
                "item_id": "C",
                "exposure_denominator": 120,
                "order_count": 20,
            },
        ],
        max_rows=100,
        truncated=False,
        context={
            "period_start": "2026-08-01",
            "period_end": "2026-08-03",
            "time_column": "stat_date",
            "required_dimensions": ["item_id"],
            "denominator_columns": ["exposure_denominator"],
            "sample_size_columns": ["order_count"],
        },
        now=datetime(2026, 8, 5, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert quality["freshness"] == {
        "field": "stat_date",
        "latest": "2026-08-03",
        "lag_days": 2,
    }
    assert quality["period_coverage"]["coverage_ratio"] == 1.0
    assert quality["period_coverage"]["complete"] is True
    assert quality["zero_denominators"] == {"exposure_denominator": 1}
    assert quality["small_samples"]["columns"]["order_count"] == {
        "affected_rows": 2,
        "minimum": 5.0,
    }
    assert quality["dimensions"]["missing"] == []


def test_quality_analyzer_uses_explicit_aggregate_coverage_columns():
    quality = analyze_query_quality(
        columns=[
            "pay_amt",
            "data_period_start",
            "data_period_end",
            "covered_days",
            "data_freshness",
        ],
        rows=[
            {
                "pay_amt": 100,
                "data_period_start": "2026-08-01",
                "data_period_end": "2026-08-07",
                "covered_days": 7,
                "data_freshness": "2026-08-07",
            }
        ],
        max_rows=100,
        truncated=False,
        context={"period_start": "2026-08-01", "period_end": "2026-08-07"},
        now=datetime(2026, 8, 8, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert quality["freshness"]["latest"] == "2026-08-07"
    assert quality["period_coverage"]["covered_days"] == 7
    assert quality["period_coverage"]["complete"] is True


def test_quality_analyzer_reports_unavailable_time_and_missing_dimensions():
    quality = analyze_query_quality(
        columns=["pay_amt"],
        rows=[{"pay_amt": 100}],
        max_rows=100,
        truncated=False,
        context={"required_dimensions": ["item_id"]},
    )

    assert quality["freshness"] is None
    assert quality["period_coverage"]["complete"] is None
    assert quality["dimensions"]["missing"] == ["item_id"]
    assert "结果中没有可识别的数据时间，无法计算数据更新时间。" in quality["warnings"]
