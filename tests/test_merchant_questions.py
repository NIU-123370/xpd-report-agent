from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
QUESTION_LIBRARY = ROOT / "knowledge" / "merchant_questions.yaml"
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "merchant_questions"


def _load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_merchant_question_library_has_stable_unique_contract():
    payload = _load_yaml(QUESTION_LIBRARY)
    questions = payload["questions"]

    assert payload["version"] == "1.1"
    assert payload["defaults"]["unspecified_comparison"] == "等长上一周期"
    assert len(questions) >= 30
    assert len({item["id"] for item in questions}) == len(questions)
    assert set(payload["answer_statuses"]) == {
        "success",
        "no_data",
        "undefined",
        "insufficient_data",
        "partial_data",
        "permission_denied",
    }

    for item in questions:
        assert item["analysis_type"] in {"query", "comparison", "diagnosis"}
        assert item["utterances"]
        assert item["canonical_question"]
        assert item["expected_output"]
        assert item["answer"]["template"]
        assert item["answer"]["evidence"]
        assert item["answer"]["guardrails"]
        assert item["answer"]["fallback"]
        clarification = item["clarification"]
        if clarification is not None:
            assert clarification["when"]
            assert clarification["question"]
            assert len(clarification["choices"]) <= 4


def test_merchant_question_regression_fixtures_are_valid():
    fixture_paths = sorted(FIXTURE_DIR.glob("*.yaml"))
    assert {path.name for path in fixture_paths} == {
        "boundary_cases.yaml",
        "clarification_cases.yaml",
        "export_cases.yaml",
        "standard_cases.yaml",
    }

    case_ids = []
    for path in fixture_paths:
        cases = _load_yaml(path)["cases"]
        assert cases
        for case in cases:
            case_ids.append(case["id"])
            assert case["input"]
            assert isinstance(case["must_clarify"], bool)
            assert len(case.get("expected_choices", [])) <= 4
            expected_answer = case.get("expected_answer")
            if expected_answer is not None:
                assert expected_answer["status"] in {
                    "success",
                    "no_data",
                    "undefined",
                    "insufficient_data",
                    "partial_data",
                    "permission_denied",
                }
                assert expected_answer["must_include"]
                assert expected_answer["must_not_claim"]

    assert len(case_ids) == len(set(case_ids))


def test_local_raw_question_log_is_not_committed():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "data/merchant_questions.local.jsonl" in gitignore.splitlines()
