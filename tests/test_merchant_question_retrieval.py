from xpd_report_agent.api.main import ChatRequest, build_payload
from xpd_report_agent.api.merchant_questions import (
    match_merchant_questions,
    merchant_question_prompt,
)
from xpd_report_agent.api.prompts import CHINESE_REASONING_REMINDER
from xpd_report_agent.api.sessions import report_system_prompt


def test_matcher_returns_only_close_bounded_examples():
    matches = match_merchant_questions("昨天成交额是多少？")

    assert [match["id"] for match in matches] == ["mq_001"]
    assert len(matches) <= 3
    assert match_merchant_questions("你好，给我讲个笑话") == []


def test_matcher_handles_boundary_and_natural_paraphrase_questions():
    boundary = match_merchant_questions("今天比昨天怎么样？")
    refund = match_merchant_questions("退款率怎么突然升高了？")

    assert boundary[0]["id"] == "mq_021"
    assert refund[0]["id"] == "mq_008"
    assert boundary[0]["answer"]["guardrails"]


def test_prompt_contains_compact_guidance_not_the_whole_library():
    prompt = merchant_question_prompt("把全部明细都导出来")

    assert prompt.count("<merchant_question_guidance>") == 1
    assert '"id":"mq_027"' in prompt
    assert "案例不是数据事实来源" in prompt
    assert "utterances" not in prompt
    assert "match_score" not in prompt


def test_matcher_can_be_disabled_or_fail_open(monkeypatch, tmp_path):
    monkeypatch.setenv("XPD_MERCHANT_QUESTION_LIBRARY_ENABLED", "false")
    assert match_merchant_questions("昨天成交额是多少？") == []

    monkeypatch.setenv("XPD_MERCHANT_QUESTION_LIBRARY_ENABLED", "true")
    monkeypatch.setenv(
        "XPD_MERCHANT_QUESTION_LIBRARY_PATH", str(tmp_path / "missing.yaml")
    )
    assert match_merchant_questions("昨天成交额是多少？") == []


def test_shared_session_prompt_injects_once_and_keeps_language_rule_last():
    prompt = report_system_prompt(user_message="昨天成交额是多少？")

    assert prompt.count("<merchant_question_guidance>") == 1
    assert '"id":"mq_001"' in prompt
    assert prompt.endswith(CHINESE_REASONING_REMINDER)


def test_legacy_payload_reuses_matcher_without_rewriting_user_message():
    payload = build_payload(ChatRequest(message="昨天成交额是多少？"), stream=False)

    assert payload["messages"][-1] == {
        "role": "user",
        "content": "昨天成交额是多少？",
    }
    assert payload["messages"][0]["content"].count(
        "<merchant_question_guidance>"
    ) == 1
