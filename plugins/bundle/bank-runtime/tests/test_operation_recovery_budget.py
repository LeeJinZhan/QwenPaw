"""Recovery is local to the failed operation and always finitely bounded."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bank_runtime.gateway.recovery_budget import OperationRecoveryBudget


def document(name="report.docx", source="file-a", content=None):
    return {"artifact_type": "docx", "output_name": name,
            "source_refs": [source], "content": content or {"paragraphs": ["text"]}}


def test_independent_tools_artifacts_and_sources_have_separate_corrections():
    budget = OperationRecoveryBudget()
    first = document()
    budget.fail("artifact_generate", first, "INVALID_REQUEST")
    budget.fail("artifact_generate", first, "INVALID_REQUEST")
    assert budget.blocked_reason("artifact_generate", first)
    assert not budget.blocked_reason("chart_export", {"chart_id": "chart-a"})
    assert not budget.blocked_reason("artifact_generate", document("other.docx"))
    assert not budget.blocked_reason("artifact_generate", document(source="file-b"))


def test_changed_input_can_progress_but_cycling_back_cannot_reset_budget():
    budget = OperationRecoveryBudget()
    first = document(content={"paragraphs": "wrong"})
    fixed = document(content={"paragraphs": ["text"]})
    for _ in range(2):
        budget.fail("artifact_generate", first, "INVALID_REQUEST", diagnostic="paragraphs")
    assert not budget.blocked_reason("artifact_generate", fixed)
    budget.fail("artifact_generate", fixed, "INVALID_REQUEST", diagnostic="title")
    assert budget.blocked_reason("artifact_generate", first)
    assert not budget.blocked_reason("artifact_generate", fixed)


def test_different_error_types_do_not_exhaust_each_other():
    budget = OperationRecoveryBudget()
    payload = document()
    budget.fail("artifact_generate", payload, "INVALID_REQUEST")
    budget.fail("artifact_generate", payload, "ARTIFACT_VALIDATION_FAILED")
    assert not budget.blocked_reason("artifact_generate", payload)


def test_continually_changed_inputs_still_hit_operation_limit():
    budget = OperationRecoveryBudget(operation_limit=4)
    for index in range(4):
        payload = document(content={"paragraphs": [str(index)]})
        assert not budget.blocked_reason("artifact_generate", payload)
        budget.fail("artifact_generate", payload, "INVALID_REQUEST", diagnostic=str(index))
    assert budget.blocked_reason("artifact_generate", document(content={"paragraphs": ["new"]}))


def test_success_resolves_only_same_operation_and_does_not_reset_task_budget():
    budget = OperationRecoveryBudget(task_limit=3)
    budget.fail("artifact_generate", document(), "INVALID_REQUEST")
    budget.fail("artifact_generate", document("other.docx"), "INVALID_REQUEST")
    budget.succeed("artifact_generate", document())
    assert budget.pending_count == 1
    budget.fail("artifact_generate", document("third.docx"), "INVALID_REQUEST")
    assert budget.task_exhausted
    assert budget.blocked_reason("chart_export", {"chart_id": "new"}) == "task_recovery_budget_exhausted"


@pytest.mark.parametrize("state", ["unknown", "execution_unknown", "pending", "executing"])
def test_unknown_execution_blocks_changed_input_for_same_operation_only(state):
    budget = OperationRecoveryBudget()
    budget.fail("artifact_generate", document(), "WORKER_TIMEOUT", execution_state=state)
    assert budget.blocked_reason("artifact_generate", document(content={"paragraphs": ["changed"]})) == "execution_unknown"
    assert not budget.blocked_reason("artifact_generate", document("other.docx"))


def test_exhausted_layout_does_not_block_other_deliverables():
    budget = OperationRecoveryBudget()
    payload = {"artifact_type": "pptx", "output_name": "report.pptx"}
    budget.fail("artifact_generate", payload, "ARTIFACT_VALIDATION_FAILED", terminal_reason="renderer_exhausted")
    assert budget.blocked_reason("artifact_generate", {**payload, "content": {"slides": []}}) == "renderer_exhausted"
    assert not budget.blocked_reason("artifact_generate", document())


def test_actionable_layout_input_can_be_repaired_within_budget():
    budget = OperationRecoveryBudget()
    payload = {"artifact_type": "pptx", "output_name": "report.pptx", "content": {"slides": ["dense"]}}
    budget.fail("artifact_generate", payload, "ARTIFACT_VALIDATION_FAILED", diagnostic="page:3:title")
    assert not budget.blocked_reason("artifact_generate", {**payload, "content": {"slides": ["reflowed"]}})


def test_malformed_json_is_bounded_without_parsing_untrusted_partial_identity():
    budget = OperationRecoveryBudget()
    for _ in range(2):
        budget.fail("artifact_generate", '{"content":', "MALFORMED_ARGUMENTS")
    assert budget.blocked_reason("artifact_generate", '{"content":')
    assert not budget.blocked_reason("artifact_generate", document())
    budget.succeed("artifact_generate", document())
    assert budget.pending_count == 0
