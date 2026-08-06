from pathlib import Path

import yaml

from xpd_report_agent.api.main import ChatRequest, build_payload
from xpd_report_agent.api.metric_definitions import (
    match_metric_definitions,
    metric_definition_prompt,
)
from xpd_report_agent.api.prompts import CHINESE_REASONING_REMINDER
from xpd_report_agent.api.sessions import report_system_prompt

ROOT = Path(__file__).resolve().parents[1]
LIBRARY_PATH = ROOT / "knowledge" / "metric_definitions.yaml"


def test_metric_library_has_versioned_complete_contract():
    payload = yaml.safe_load(LIBRARY_PATH.read_text(encoding="utf-8"))
    metrics = payload["metrics"]

    assert payload["version"] == "1.0"
    assert 20 <= len(metrics) <= 30
    assert len({metric["id"] for metric in metrics}) == len(metrics)
    for metric in metrics:
        assert metric["name"]
        assert metric["aliases"]
        assert metric["formula"]
        assert metric["stage"] in {"sql", "post_query"}
        assert metric["metric_type"] in {
            "additive",
            "non_additive",
            "ratio",
            "derived",
        }
        assert metric["required_fields"]
        assert metric["allowed_grains"]
        assert metric["aggregation_rule"]
        assert isinstance(metric["warnings"], list)
        if metric["metric_type"] == "ratio":
            assert "NULLIF" in metric["formula"]
            assert metric["zero_denominator"]


def test_metric_matcher_handles_core_combined_and_irrelevant_questions():
    assert match_metric_definitions("昨天成交额是多少")[0]["id"] == "pay_amount"
    combined = match_metric_definitions("成交额环比增长多少")
    assert {metric["id"] for metric in combined} >= {"pay_amount", "relative_change"}
    assert match_metric_definitions("你好，讲个笑话") == []
    assert len(combined) <= 6


def test_ambiguous_business_word_returns_relevant_definitions():
    matches = match_metric_definitions("退款率怎么升高了")

    assert matches[0]["id"] == "amount_refund_rate"
    assert matches[0]["required_fields"] == ["refund_amt", "pay_amt"]


def test_metric_prompt_is_compact_and_requires_schema_verification():
    prompt = metric_definition_prompt("客单价和连带率")

    assert prompt.count("<metric_definition_guidance") == 1
    assert '"id":"customer_unit_price"' in prompt
    assert '"id":"items_per_order"' in prompt
    assert "先用表画像确认 required_fields" in prompt
    assert "aliases" not in prompt
    assert "match_score" not in prompt


def test_metric_library_can_be_disabled_or_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("XPD_METRIC_DEFINITION_LIBRARY_ENABLED", "false")
    assert match_metric_definitions("成交金额") == []

    monkeypatch.setenv("XPD_METRIC_DEFINITION_LIBRARY_ENABLED", "true")
    monkeypatch.setenv(
        "XPD_METRIC_DEFINITION_LIBRARY_PATH", str(tmp_path / "missing.yaml")
    )
    assert match_metric_definitions("成交金额") == []


def test_session_and_legacy_prompts_inject_metric_context_once():
    session_prompt = report_system_prompt(user_message="成交额环比增长多少")
    legacy_payload = build_payload(
        ChatRequest(message="成交额环比增长多少"), stream=False
    )

    assert session_prompt.count("<metric_definition_guidance") == 1
    assert session_prompt.endswith(CHINESE_REASONING_REMINDER)
    assert legacy_payload["messages"][0]["content"].count(
        "<metric_definition_guidance"
    ) == 1
    assert legacy_payload["messages"][-1]["content"] == "成交额环比增长多少"
