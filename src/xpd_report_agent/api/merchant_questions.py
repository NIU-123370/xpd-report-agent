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

DEFAULT_LIBRARY_PATH = PROJECT_ROOT / "knowledge" / "merchant_questions.yaml"
PACKAGED_LIBRARY_PATH = Path(__file__).resolve().parents[1] / "knowledge" / "merchant_questions.yaml"
MAX_MATCHES = 3
DEFAULT_MIN_SCORE = 0.42
_PUNCTUATION = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")
_NUMBER_ALIASES = {"七天": "7天", "三十天": "30天", "九十天": "90天"}


def _env_enabled(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def merchant_question_library_path() -> Path:
    configured = os.getenv("XPD_MERCHANT_QUESTION_LIBRARY_PATH", "").strip()
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
    if not isinstance(payload, dict) or not isinstance(payload.get("questions"), list):
        return {}
    return payload


def load_merchant_question_library() -> dict[str, Any]:
    """Load the optional library and fail open if it is unavailable or invalid."""

    path = merchant_question_library_path()
    try:
        modified_ns = path.stat().st_mtime_ns
    except OSError:
        return {}
    return _load_library(str(path.resolve()), modified_ns)


def _normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    for source, target in _NUMBER_ALIASES.items():
        text = text.replace(source, target)
    return _PUNCTUATION.sub("", text)


def _ngrams(text: str, size: int) -> set[str]:
    if len(text) <= size:
        return {text} if text else set()
    return {text[index : index + size] for index in range(len(text) - size + 1)}


def _dice(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return 2 * len(left & right) / (len(left) + len(right))


def _text_similarity(query: str, candidate: str) -> float:
    if not query or not candidate:
        return 0.0
    if query == candidate:
        return 1.0
    containment = 0.0
    if query in candidate or candidate in query:
        length_ratio = min(len(query), len(candidate)) / max(len(query), len(candidate))
        containment = 0.80 + 0.16 * length_ratio
    sequence = SequenceMatcher(None, query, candidate, autojunk=False).ratio()
    bigram = _dice(_ngrams(query, 2), _ngrams(candidate, 2))
    trigram = _dice(_ngrams(query, 3), _ngrams(candidate, 3))
    return max(containment, 0.45 * sequence + 0.35 * bigram + 0.20 * trigram)


def _question_score(query: str, question: dict[str, Any]) -> float:
    utterances = question.get("utterances")
    candidates = list(utterances) if isinstance(utterances, list) else []
    candidates.append(question.get("canonical_question", ""))
    score = max(
        (_text_similarity(query, _normalize(candidate)) for candidate in candidates),
        default=0.0,
    )
    feature_hits = 0
    for feature in (
        question.get("category"),
        *(question.get("metrics") or []),
        *(question.get("dimensions") or []),
    ):
        normalized_feature = _normalize(feature)
        if len(normalized_feature) >= 2 and normalized_feature in query:
            feature_hits += 1
    return min(1.0, score + min(feature_hits, 2) * 0.025)


def _bounded_top_k(value: int | None) -> int:
    if value is None:
        try:
            value = int(os.getenv("XPD_MERCHANT_QUESTION_TOP_K", str(MAX_MATCHES)))
        except ValueError:
            value = MAX_MATCHES
    return max(1, min(MAX_MATCHES, int(value)))


def match_merchant_questions(
    user_message: str | None,
    *,
    top_k: int | None = None,
    min_score: float = DEFAULT_MIN_SCORE,
) -> list[dict[str, Any]]:
    """Return at most three close examples without exposing the whole library."""

    if not _env_enabled("XPD_MERCHANT_QUESTION_LIBRARY_ENABLED", True):
        return []
    query = _normalize(user_message)
    if len(query) < 2:
        return []
    ranked = []
    for question in load_merchant_question_library().get("questions") or []:
        if not isinstance(question, dict):
            continue
        score = _question_score(query, question)
        if score >= min_score:
            ranked.append((score, question))
    ranked.sort(key=lambda item: (-item[0], str(item[1].get("id") or "")))
    if not ranked:
        return []
    best_score = ranked[0][0]
    result = []
    for score, question in ranked:
        if len(result) >= _bounded_top_k(top_k) or score < best_score - 0.20:
            break
        result.append({**question, "match_score": round(score, 4)})
    return result


def _prompt_question(match: dict[str, Any]) -> dict[str, Any]:
    answer = match.get("answer")
    return {
        "id": match.get("id"),
        "analysis_type": match.get("analysis_type"),
        "canonical_question": match.get("canonical_question"),
        "metrics": match.get("metrics") or [],
        "dimensions": match.get("dimensions") or [],
        "clarification": match.get("clarification")
        if isinstance(match.get("clarification"), dict)
        else None,
        "answer": {
            "template": answer.get("template"),
            "evidence": answer.get("evidence") or [],
            "guardrails": answer.get("guardrails") or [],
            "fallback": answer.get("fallback"),
        }
        if isinstance(answer, dict)
        else None,
    }


def merchant_question_prompt(user_message: str | None) -> str:
    matches = match_merchant_questions(user_message)
    if not matches:
        return ""
    compact_matches = [_prompt_question(match) for match in matches]
    return (
        "<merchant_question_guidance>\n"
        "以下是服务端从商家问题库检索出的少量相似案例，仅用于确定分析类型、澄清条件和答案约束。\n"
        "使用规则：\n"
        "1. 当前用户消息和已确认的会话上下文优先；案例不匹配时忽略，不得生搬硬套。\n"
        "2. 案例不是数据事实来源；模板占位符必须替换为数据库真实结果，缺失数据不得补造。\n"
        "3. 只有歧义会实质改变 SQL、指标或结论时才澄清；已确认内容不要重复询问。\n"
        "4. 最终回答遵守 evidence、guardrails 和 fallback，并继续执行必要的只读数据库流程。\n"
        f"匹配案例：{json.dumps(compact_matches, ensure_ascii=False, separators=(',', ':'))}\n"
        "</merchant_question_guidance>"
    )
