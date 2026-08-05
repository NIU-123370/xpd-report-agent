from __future__ import annotations

import json
import threading

import pytest

from xpd_report_agent.api.agent_runs import (
    AgentRunStore,
    AgentRunStoreError,
    IdempotencyConflictError,
    InvalidRunTransitionError,
    RunInputNotAllowedError,
    RunRetryNotAllowedError,
    default_agent_run_state_path,
    run_retry_attempt_count,
    stable_run_id,
    validate_idempotency_key,
)


def _create(store: AgentRunStore, **overrides):
    fields = {
        "owner_scope": "owner-a",
        "session_id": "xpd_owner-a_session",
        "idempotency_key": "request-001",
        "request": {"message": "分析最近七天", "format": "markdown"},
    }
    fields.update(overrides)
    return store.create_or_get(**fields)


def test_default_path_uses_hermes_home_and_can_be_overridden(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("XPD_AGENT_RUN_STATE_PATH", raising=False)
    assert default_agent_run_state_path() == tmp_path / "xpd-report-agent" / "agent-runs.json"

    custom = tmp_path / "custom" / "runs.json"
    monkeypatch.setenv("XPD_AGENT_RUN_STATE_PATH", str(custom))
    assert default_agent_run_state_path() == custom


@pytest.mark.parametrize("value", [None, "", "contains space", "line\nbreak", "中文"])
def test_idempotency_key_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        validate_idempotency_key(value)


def test_idempotency_key_has_bounded_length():
    assert validate_idempotency_key("a" * 255) == "a" * 255
    with pytest.raises(ValueError):
        validate_idempotency_key("a" * 256)


def test_create_is_stable_durable_and_canonical(tmp_path):
    path = tmp_path / "runs.json"
    first_store = AgentRunStore(path)
    first, created = _create(first_store, request={"b": [2, 1], "a": "same"})
    assert created is True
    assert first["run_id"] == stable_run_id("owner-a", "xpd_owner-a_session", "request-001")
    assert first["status"] == "pending"
    assert first["attempt_count"] == 0

    second_store = AgentRunStore(path)
    second, created = _create(second_store, request={"a": "same", "b": [2, 1]})
    assert created is False
    assert second == first

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["runs"][first["run_id"]]["request"] == {"a": "same", "b": [2, 1]}


def test_same_key_with_different_request_conflicts(tmp_path):
    store = AgentRunStore(tmp_path / "runs.json")
    _create(store, request={"message": "first"})
    with pytest.raises(IdempotencyConflictError):
        _create(store, request={"message": "second"})


def test_key_is_scoped_by_owner_and_session(tmp_path):
    store = AgentRunStore(tmp_path / "runs.json")
    first, _ = _create(store)
    other_owner, _ = _create(store, owner_scope="owner-b")
    other_session, _ = _create(store, session_id="xpd_owner-a_other")
    assert len({first["run_id"], other_owner["run_id"], other_session["run_id"]}) == 3


def test_query_requires_owner_and_public_record_hides_internal_data(tmp_path):
    store = AgentRunStore(tmp_path / "runs.json")
    run, _ = _create(store, request_id="caller-request-42")

    assert store.get_owned(run["run_id"], "owner-b") is None
    public = store.get_public_owned(run["run_id"], "owner-a")
    assert public["request_id"] == "caller-request-42"
    assert public["idempotency_key"] == "request-001"
    assert "owner_scope" not in public
    assert "request" not in public
    assert "request_hash" not in public
    assert "checkpoint" not in public
    assert "result" in public
    assert "result" not in store.public_run(run, include_result=False)


def test_status_transitions_attempts_results_and_errors(tmp_path):
    store = AgentRunStore(tmp_path / "runs.json")
    run, _ = _create(store)

    running = store.mark_running(run["run_id"], "owner-a")
    assert running["status"] == "running"
    assert running["attempt_count"] == 1
    assert running["started_at"]
    assert running["updated_at"] >= running["created_at"]

    succeeded = store.mark_succeeded(
        run["run_id"], "owner-a", result={"answer": "完成"}
    )
    assert succeeded["status"] == "succeeded"
    assert succeeded["result"] == {"answer": "完成"}
    assert succeeded["error"] is None
    assert succeeded["completed_at"]
    with pytest.raises(InvalidRunTransitionError):
        store.mark_running(run["run_id"], "owner-a")

    failed_run, _ = _create(store, idempotency_key="request-002")
    store.mark_running(failed_run["run_id"], "owner-a")
    failed = store.mark_failed(
        failed_run["run_id"], "owner-a", error={"code": "TEMPORARY"}
    )
    assert failed["status"] == "failed"
    assert failed["error"] == {"code": "TEMPORARY"}
    assert failed["result"] is None
    assert failed["completed_at"]


def test_retry_preserves_run_id_and_respects_attempt_limit(tmp_path):
    store = AgentRunStore(tmp_path / "runs.json")
    run, _ = _create(store)
    store.mark_running(run["run_id"], "owner-a")
    store.mark_failed(run["run_id"], "owner-a", error="temporary")

    retried = store.retry(run["run_id"], "owner-a", max_attempts=2)
    assert retried["run_id"] == run["run_id"]
    assert retried["status"] == "pending"
    assert retried["attempt_count"] == 1
    assert retried["error"] is None
    assert retried["completed_at"] is None

    store.mark_running(run["run_id"], "owner-a")
    store.mark_failed(run["run_id"], "owner-a", error="still failing")
    with pytest.raises(RunRetryNotAllowedError):
        store.retry(run["run_id"], "owner-a", max_attempts=2)

    with pytest.raises(RunRetryNotAllowedError):
        store.retry(_create(store, idempotency_key="request-003")[0]["run_id"], "owner-a", max_attempts=2)


def test_waiting_input_is_durable_and_identical_input_requeues_same_run(tmp_path):
    path = tmp_path / "runs.json"
    store = AgentRunStore(path)
    run, _ = _create(store)
    store.mark_running(run["run_id"], "owner-a")
    waiting = store.mark_waiting_input(
        run["run_id"],
        "owner-a",
        clarification={
            "clarification_id": "clarify_metric",
            "question": "销量按件数还是订单数？",
            "choices": ["件数", "订单数"],
        },
    )

    assert waiting["status"] == "waiting_input"
    assert waiting["clarification"]["question"] == "销量按件数还是订单数？"
    assert store.public_run(waiting)["clarification"]["choices"] == ["件数", "订单数"]
    assert AgentRunStore(path).list_resumable(max_attempts=2) == []

    resumed, accepted = AgentRunStore(path).resume_with_input(
        run["run_id"],
        "owner-a",
        idempotency_key="clarification-answer-001",
        answer="按件数",
        baseline_message_count=2,
        artifact_ids=["artifact-before-answer"],
    )
    assert accepted is True
    assert resumed["run_id"] == run["run_id"]
    assert resumed["status"] == "pending"
    assert resumed["clarification"] is None
    assert resumed["checkpoint"]["pending_input"] == {
        "clarification_id": "clarify_metric",
        "question": "销量按件数还是订单数？",
        "answer": "按件数",
    }
    assert run_retry_attempt_count(resumed) == 0
    assert {item["run_id"] for item in AgentRunStore(path).list_resumable(max_attempts=2)} == {
        run["run_id"]
    }

    duplicate, accepted = AgentRunStore(path).resume_with_input(
        run["run_id"],
        "owner-a",
        idempotency_key="clarification-answer-001",
        answer="按件数",
        baseline_message_count=999,
        artifact_ids=[],
    )
    assert accepted is False
    assert duplicate == resumed

    with pytest.raises(IdempotencyConflictError):
        AgentRunStore(path).replay_input(
            run["run_id"],
            "owner-a",
            idempotency_key="clarification-answer-001",
            answer="按订单数",
        )
    with pytest.raises(RunInputNotAllowedError):
        AgentRunStore(path).resume_with_input(
            run["run_id"],
            "owner-a",
            idempotency_key="clarification-answer-002",
            answer="按订单数",
            baseline_message_count=2,
            artifact_ids=[],
        )


def test_checkpoint_is_merged_persisted_and_not_public(tmp_path):
    path = tmp_path / "runs.json"
    store = AgentRunStore(path)
    run, _ = _create(store)
    store.set_checkpoint(run["run_id"], "owner-a", baseline_message_count=4)
    updated = store.update_checkpoint(
        run["run_id"], "owner-a", artifact_ids=["artifact-1"]
    )
    assert updated["checkpoint"] == {
        "baseline_message_count": 4,
        "artifact_ids": ["artifact-1"],
    }

    reloaded = AgentRunStore(path).get_owned(run["run_id"], "owner-a")
    assert reloaded["checkpoint"] == updated["checkpoint"]
    assert "checkpoint" not in store.public_run(reloaded)


def test_list_resumable_only_returns_eligible_pending_and_running(tmp_path):
    store = AgentRunStore(tmp_path / "runs.json")
    pending, _ = _create(store, idempotency_key="request-pending")
    running, _ = _create(store, idempotency_key="request-running")
    failed, _ = _create(store, idempotency_key="request-failed")
    succeeded, _ = _create(store, idempotency_key="request-succeeded")
    other_owner, _ = _create(
        store, owner_scope="owner-b", idempotency_key="request-other-owner"
    )
    store.mark_running(running["run_id"], "owner-a")
    store.mark_running(failed["run_id"], "owner-a")
    store.mark_failed(failed["run_id"], "owner-a", error="no")
    store.mark_running(succeeded["run_id"], "owner-a")
    store.mark_succeeded(succeeded["run_id"], "owner-a", result={})

    all_ids = {run["run_id"] for run in store.list_resumable(max_attempts=2)}
    assert all_ids == {pending["run_id"], running["run_id"], other_owner["run_id"]}
    owner_ids = {
        run["run_id"]
        for run in store.list_resumable(max_attempts=1, owner_scope="owner-a")
    }
    assert owner_ids == {pending["run_id"], running["run_id"]}


def test_concurrent_create_only_creates_once(tmp_path):
    store = AgentRunStore(tmp_path / "runs.json")
    barrier = threading.Barrier(8)
    results: list[bool] = []

    def create() -> None:
        barrier.wait()
        _, created = _create(store)
        results.append(created)

    threads = [threading.Thread(target=create) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(True) == 1
    assert results.count(False) == 7


def test_pending_run_can_only_be_claimed_once_across_store_instances(tmp_path):
    path = tmp_path / "runs.json"
    first_store = AgentRunStore(path)
    second_store = AgentRunStore(path)
    run, _ = _create(first_store)

    first, first_claimed = first_store.claim_pending(run["run_id"], "owner-a")
    second, second_claimed = second_store.claim_pending(run["run_id"], "owner-a")

    assert first_claimed is True
    assert second_claimed is False
    assert first["status"] == second["status"] == "running"
    assert first["attempt_count"] == second["attempt_count"] == 1


def test_execution_claim_is_exclusive_across_store_instances(tmp_path):
    path = tmp_path / "runs.json"
    first_store = AgentRunStore(path)
    second_store = AgentRunStore(path)

    with first_store.execution_claim("run:shared") as first_claimed:
        with second_store.execution_claim("run:shared") as second_claimed:
            assert first_claimed is True
            assert second_claimed is False

    with second_store.execution_claim("run:shared") as claimed_after_release:
        assert claimed_after_release is True


def test_corrupt_store_fails_closed(tmp_path):
    path = tmp_path / "runs.json"
    path.write_text("not-json", encoding="utf-8")
    with pytest.raises(AgentRunStoreError):
        _create(AgentRunStore(path))

    path.write_text("[]", encoding="utf-8")
    with pytest.raises(AgentRunStoreError):
        _create(AgentRunStore(path))
