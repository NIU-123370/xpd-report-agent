from __future__ import annotations

import json
import os
import re
import unicodedata
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from xpd_report_agent.paths import PROJECT_ROOT

DEFAULT_LIBRARY_PATH = PROJECT_ROOT / "knowledge" / "metric_definitions.yaml"
PACKAGED_LIBRARY_PATH = Path(__file__).resolve().parents[1] / "knowledge" / "metric_definitions.yaml"
MAX_MATCHES = 6
DEFAULT_MIN_SCORE = 0.38
_PUNCTUATION = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


def _env_enabled(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def metric_definition_library_path() -> Path:
    configured = os.getenv("XPD_METRIC_DEFINITION_LIBRARY_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    return DEFAULT_LIBRARY_PATH if DEFAULT_LIBRARY_PATH.is_file() else PACKAGED_LIBRARY_PATH


@lru_cache(maxsize=8)
def _load_library(path_value: str, modified_ns: int) -> dict[str, Any]:
    del modified_ns
    try:
        payload = yaml.safe_load(Path(path_value).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return {}
    if not isinstance(payload, dict) or not isinstance(payload.get("metrics"), list):
        return {}
    return payload


def load_metric_definition_library() -> dict[str, Any]:
    path = metric_definition_library_path()
    try:
        modified_ns = path.stat().st_mtime_ns
    except OSError:
        return {}
    return _load_library(str(path.resolve()), modified_ns)


def _normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    return _PUNCTUATION.sub("", text)


def _ngrams(text: str, size: int) -> set[str]:
    if len(text) <= size:
        return {text} if text else set()
    return {text[index : index + size] for index in range(len(text) - size + 1)}


def _dice(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return 2 * len(left & right) / (len(left) + len(right))


def _similarity(query: str, candidate: str) -> float:
    if not query or not candidate:
        return 0.0
    if query == candidate:
        return 1.0
    containment = 0.0
    if query in candidate or candidate in query:
        ratio = min(len(query), len(candidate)) / max(len(query), len(candidate))
        containment = 0.80 + 0.16 * ratio
    sequence = SequenceMatcher(None, query, candidate, autojunk=False).ratio()
    bigram = _dice(_ngrams(query, 2), _ngrams(candidate, 2))
    return max(containment, 0.55 * sequence + 0.45 * bigram)


def _metric_score(query: str, metric: dict[str, Any]) -> float:
    candidates = [metric.get("name", ""), *(metric.get("aliases") or [])]
    score = max(
        (_similarity(query, _normalize(candidate)) for candidate in candidates),
        default=0.0,
    )
    category = _normalize(metric.get("category"))
    if len(category) >= 2 and category in query:
        score += 0.025
    return min(1.0, score)


def _bounded_top_k(value: int | None) -> int:
    if value is None:
        try:
            value = int(os.getenv("XPD_METRIC_DEFINITION_TOP_K", str(MAX_MATCHES)))
        except ValueError:
            value = MAX_MATCHES
    return max(1, min(MAX_MATCHES, int(value)))


def match_metric_definitions(
    user_message: str | None,
    *,
    top_k: int | None = None,
    min_score: float = DEFAULT_MIN_SCORE,
) -> list[dict[str, Any]]:
    if not _env_enabled("XPD_METRIC_DEFINITION_LIBRARY_ENABLED", True):
        return []
    query = _normalize(user_message)
    if len(query) < 2:
        return []
    ranked = []
    for metric in load_metric_definition_library().get("metrics") or []:
        if not isinstance(metric, dict):
            continue
        score = _metric_score(query, metric)
        if score >= min_score:
            ranked.append((score, metric))
    ranked.sort(key=lambda item: (-item[0], str(item[1].get("id") or "")))
    if not ranked:
        return []
    best_score = ranked[0][0]
    result = []
    for score, metric in ranked:
        if len(result) >= _bounded_top_k(top_k) or score < best_score - 0.25:
            break
        result.append({**metric, "match_score": round(score, 4)})
    return result


def _prompt_metric(metric: dict[str, Any]) -> dict[str, Any]:
    return {
        key: metric.get(key)
        for key in (
            "id",
            "name",
            "definition",
            "stage",
            "formula",
            "unit",
            "metric_type",
            "required_fields",
            "preferred_tables",
            "allowed_grains",
            "aggregation_rule",
            "zero_denominator",
            "warnings",
        )
    }


def metric_definition_prompt(user_message: str | None) -> str:
    matches = match_metric_definitions(user_message)
    if not matches:
        return ""
    library = load_metric_definition_library()
    compact_metrics = [_prompt_metric(metric) for metric in matches]
    governance = library.get("governance") if isinstance(library, dict) else {}
    return (
        f"<metric_definition_guidance version={json.dumps(str(library.get('version') or ''))}>\n"
        "以下指标定义由服务端按当前问题检索，是本轮指标口径的优先参考，但不代表字段一定存在。\n"
        "使用规则：\n"
        "1. 先用表画像确认 required_fields、粒度和表语义；字段缺失时不得计算或用相似字段替代。\n"
        "2. 严格遵守 formula、aggregation_rule、zero_denominator 和 warnings；比率不得平均明细比率。\n"
        "3. 同一用户词命中多种口径且会改变结果时必须澄清；已确认口径不得重复询问。\n"
        "4. SQL 仍须通过服务端只读校验；最终用户可见答案不得展示 SQL。\n"
        f"全局规则：{json.dumps(governance or {}, ensure_ascii=False, separators=(',', ':'))}\n"
        f"匹配指标：{json.dumps(compact_metrics, ensure_ascii=False, separators=(',', ':'))}\n"
        "</metric_definition_guidance>"
    )
