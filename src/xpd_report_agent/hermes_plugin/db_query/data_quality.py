from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

_TIME_NAME = re.compile(r"(?:^|_)(?:date|day|time|timestamp|at)(?:$|_)", re.I)
_AUTO_SAMPLE_NAME = re.compile(
    r"(?:^sample_size$|_sample_size$|^order_count$|^buyer_count$)", re.I
)
_AUTO_DENOMINATOR_NAME = re.compile(r"(?:^denominator$|_denominator$)", re.I)
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def normalize_quality_context(value: Any) -> dict[str, Any]:
    """Bound optional semantic hints before using them in quality checks."""

    raw = value if isinstance(value, dict) else {}

    def names(key: str, limit: int = 20) -> list[str]:
        items = raw.get(key)
        if not isinstance(items, list):
            return []
        return [str(item) for item in items if _NAME.fullmatch(str(item))][:limit]

    time_column = str(raw.get("time_column") or "").strip()
    if not _NAME.fullmatch(time_column):
        time_column = ""
    return {
        "period_start": _parse_date(raw.get("period_start")),
        "period_end": _parse_date(raw.get("period_end")),
        "time_column": time_column,
        "required_dimensions": names("required_dimensions", 10),
        "denominator_columns": names("denominator_columns"),
        "sample_size_columns": names("sample_size_columns"),
        "small_sample_threshold": _bounded_int(
            raw.get("small_sample_threshold"), default=30, minimum=1, maximum=10_000
        ),
    }


def analyze_query_quality(
    *,
    columns: list[str],
    rows: list[dict[str, Any]],
    max_rows: int,
    truncated: bool,
    context: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    hints = normalize_quality_context(context)
    normalized_columns = {column.lower(): column for column in columns}
    null_counts = {
        column: sum(row.get(column) is None for row in rows) for column in columns
    }
    null_counts = {column: count for column, count in null_counts.items() if count}

    required_dimensions = hints["required_dimensions"]
    missing_dimensions = [
        dimension
        for dimension in required_dimensions
        if dimension.lower() not in normalized_columns
    ]
    present_dimensions = [
        normalized_columns[dimension.lower()]
        for dimension in required_dimensions
        if dimension.lower() in normalized_columns
    ]

    denominator_columns = _resolve_columns(
        columns,
        hints["denominator_columns"],
        auto_pattern=_AUTO_DENOMINATOR_NAME,
    )
    zero_denominators = {
        column: sum(_is_zero(row.get(column)) for row in rows)
        for column in denominator_columns
    }
    zero_denominators = {
        column: count for column, count in zero_denominators.items() if count
    }

    sample_columns = _resolve_columns(
        columns,
        hints["sample_size_columns"],
        auto_pattern=_AUTO_SAMPLE_NAME,
    )
    threshold = hints["small_sample_threshold"]
    small_samples = {}
    for column in sample_columns:
        values = [
            number
            for row in rows
            if (number := _number(row.get(column))) is not None and number >= 0
        ]
        small = [number for number in values if number < threshold]
        if small:
            small_samples[column] = {
                "affected_rows": len(small),
                "minimum": min(small),
            }

    time_column = _choose_time_column(columns, hints["time_column"])
    observed_dates = _observed_dates(rows, time_column)
    freshness = _freshness(
        rows,
        columns,
        time_column=time_column,
        observed_dates=observed_dates,
        now=now,
    )
    coverage = _coverage(
        rows,
        columns,
        observed_dates=observed_dates,
        requested_start=hints["period_start"],
        requested_end=hints["period_end"],
    )

    warnings = []
    if not rows:
        warnings.append("查询结果为空，请检查统计周期和筛选条件。")
    if truncated:
        warnings.append(
            f"结果超过 {max_rows} 行，仅返回前 {max_rows} 行，不能视为完整明细。"
        )
    if null_counts:
        warnings.append("返回结果包含空值，相关计算需说明处理方式。")
    if zero_denominators:
        warnings.append("检测到零分母，相关比率不可计算。")
    if small_samples:
        warnings.append(f"检测到样本量低于 {threshold} 的结果，相关结论需谨慎。")
    if missing_dimensions:
        warnings.append("结果缺少分析所需维度：" + "、".join(missing_dimensions))
    if coverage.get("complete") is False:
        warnings.append("返回数据未完整覆盖请求周期。")
    elif (
        hints["period_start"]
        and hints["period_end"]
        and coverage.get("complete") is None
    ):
        warnings.append("结果缺少日期或覆盖天数，无法计算周期覆盖率。")
    if freshness is None:
        warnings.append("结果中没有可识别的数据时间，无法计算数据更新时间。")
    elif freshness["lag_days"] < 0:
        warnings.append("检测到晚于当前日期的数据时间，请检查时区或源数据日期。")

    return {
        "scope": "returned_rows",
        "returned_row_count": len(rows),
        "column_count": len(columns),
        "max_rows": max_rows,
        "truncated": truncated,
        "empty_result": not rows,
        "validation_passed": True,
        "null_counts": null_counts,
        "freshness": freshness,
        "period_coverage": coverage,
        "zero_denominators": zero_denominators,
        "small_samples": {
            "threshold": threshold,
            "columns": small_samples,
        },
        "dimensions": {
            "required": required_dimensions,
            "present": present_dimensions,
            "missing": missing_dimensions,
        },
        "warnings": warnings,
    }


def _resolve_columns(
    columns: list[str], requested: list[str], *, auto_pattern: re.Pattern[str]
) -> list[str]:
    available = {column.lower(): column for column in columns}
    resolved = [available[name.lower()] for name in requested if name.lower() in available]
    for column in columns:
        if auto_pattern.search(column) and column not in resolved:
            resolved.append(column)
    return resolved


def _choose_time_column(columns: list[str], requested: str) -> str | None:
    available = {column.lower(): column for column in columns}
    if requested and requested.lower() in available:
        return available[requested.lower()]
    preferred = (
        "data_freshness",
        "data_period_end",
        "stat_date",
        "live_start_time",
        "updated_at",
        "created_at",
    )
    for name in preferred:
        if name in available:
            return available[name]
    return next((column for column in columns if _TIME_NAME.search(column)), None)


def _freshness(
    rows: list[dict[str, Any]],
    columns: list[str],
    *,
    time_column: str | None,
    observed_dates: list[date],
    now: datetime | None,
) -> dict[str, Any] | None:
    explicit = next(
        (column for column in columns if column.lower() == "data_freshness"), None
    )
    field = explicit or time_column
    dates = _observed_dates(rows, field) if explicit else observed_dates
    if not field or not dates:
        return None
    latest = max(dates)
    current = (now or datetime.now(ZoneInfo("Asia/Shanghai"))).date()
    return {
        "field": field,
        "latest": latest.isoformat(),
        "lag_days": (current - latest).days,
    }


def _coverage(
    rows: list[dict[str, Any]],
    columns: list[str],
    *,
    observed_dates: list[date],
    requested_start: date | None,
    requested_end: date | None,
) -> dict[str, Any]:
    available = {column.lower(): column for column in columns}
    observed_start = _first_date(rows, available.get("data_period_start"))
    observed_end = _first_date(rows, available.get("data_period_end"))
    if observed_dates:
        observed_start = observed_start or min(observed_dates)
        observed_end = observed_end or max(observed_dates)

    covered_days = _first_int(rows, available.get("covered_days"))
    if covered_days is None and observed_dates:
        covered_days = len(set(observed_dates))
    expected_days = None
    if requested_start and requested_end and requested_end >= requested_start:
        expected_days = (requested_end - requested_start).days + 1

    ratio = None
    complete = None
    if expected_days is not None and covered_days is not None:
        ratio = min(1.0, covered_days / expected_days)
        complete = bool(
            ratio >= 1
            and observed_start
            and observed_end
            and observed_start <= requested_start
            and observed_end >= requested_end
        )
    return {
        "requested_start": requested_start.isoformat() if requested_start else None,
        "requested_end": requested_end.isoformat() if requested_end else None,
        "observed_start": observed_start.isoformat() if observed_start else None,
        "observed_end": observed_end.isoformat() if observed_end else None,
        "covered_days": covered_days,
        "expected_days": expected_days,
        "coverage_ratio": ratio,
        "complete": complete,
        "basis": "returned_rows_or_explicit_coverage_columns",
    }


def _observed_dates(rows: list[dict[str, Any]], column: str | None) -> list[date]:
    if not column:
        return []
    return [parsed for row in rows if (parsed := _parse_date(row.get(column)))]


def _first_date(rows: list[dict[str, Any]], column: str | None) -> date | None:
    values = _observed_dates(rows, column)
    return values[0] if values else None


def _first_int(rows: list[dict[str, Any]], column: str | None) -> int | None:
    if not column:
        return None
    for row in rows:
        try:
            return int(row.get(column))
        except (TypeError, ValueError):
            continue
    return None


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_zero(value: Any) -> bool:
    number = _number(value)
    return number == 0 if number is not None else False


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, number))
