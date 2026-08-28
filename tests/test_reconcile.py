from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from repo_detective.agent import InvestigationAgent
from repo_detective.config import Settings
from repo_detective.models import InvestigationStatus, LLMResponse, LLMToolCall, ToolResult, ToolResultStatus
from repo_detective.report import ReportRenderer
from repo_detective.storage import InvestigationStore


class FakeLLM:
    model = "fake"

    def __init__(self, responses):
        self.responses = list(responses)

    def choose_tool(self, *, messages, tools):
        return self.responses.pop(0)


class FakeTools:
    definitions = [{"type": "function", "function": {"name": "fake_tool", "description": "f", "parameters": {"type": "object", "properties": {}}}}]
    names = {"fake_tool"}

    def execute(self, name, *, investigation, step_id, arguments):
        return ToolResult(ToolResultStatus.SUCCESS, "Fake fact was inspected")


class ReconcileInterruptedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = InvestigationStore(root / "test.db")
        self.settings = Settings(root, root / "test.db", root / "reports", "k", "https://x/v1", "m", None,
                                 "https://api.github.com", "2026-03-10", 5, 60, 50, 1000)
        self.settings.reports_dir.mkdir()
        self.investigation_id = self.store.create_investigation(input_url="owner/repo", owner="owner", repo="repo", goal="adopt?", initial_budget=5)
        self.ev = self.store.add_evidence(
            self.investigation_id, step_id=None, source="intake", tool_name="get_repository",
            api_url="https://api.github.com/repos/owner/repo", html_url="https://github.com/owner/repo",
            request_parameters={}, http_status=200, verification_status="verified", summary="Repository exists",
            normalized={}, rate_limit_remaining=59, raw={},
        )
        self.store.save_intake(self.investigation_id, {"canonical_full_name": "owner/repo"}, canonical_full_name="owner/repo", html_url="https://github.com/owner/repo")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_interruption_after_reserving_a_call_is_closed_without_refund(self) -> None:
        self.store.reserve_llm_call(self.investigation_id, "investigation", "fake")   # process dies here
        before = self.store.get_investigation(self.investigation_id)
        counts = self.store.reconcile_interrupted(self.investigation_id)
        self.assertEqual(counts, {"interrupted_calls": 1, "interrupted_steps": 0})
        call = self.store.list_llm_calls(self.investigation_id)[-1]
        self.assertEqual(call["status"], "interrupted")
        self.assertIsNotNone(call["completed_at"])
        self.assertIn("not refunded", call["error"])
        after = self.store.get_investigation(self.investigation_id)
        self.assertEqual(after["investigation_calls_used"], before["investigation_calls_used"])
        self.assertEqual(after["remaining_budget"], before["remaining_budget"])
        self.assertEqual(after["remaining_budget"], 4)

    def test_interruption_after_creating_a_step_marks_it_as_error(self) -> None:
        call = self.store.reserve_llm_call(self.investigation_id, "investigation", "fake")
        self.store.complete_llm_call(call["id"], provider_request_id="r", input_tokens=1, output_tokens=1)
        self.store.add_step(self.investigation_id, revision=1, llm_call_number=1, action_type="github_tool", rationale="look",
                            question_to_answer="q", based_on_evidence_ids=[self.ev], tool_name="list_commits", tool_arguments={})
        # process dies before complete_step
        counts = self.store.reconcile_interrupted(self.investigation_id)
        self.assertEqual(counts, {"interrupted_calls": 0, "interrupted_steps": 1})
        step = self.store.list_steps(self.investigation_id)[-1]
        self.assertEqual(step["result_status"], "error")
        self.assertIsNotNone(step["completed_at"])
        self.assertIn("stopped before the result of this step was persisted", step["observation"])
        self.assertEqual(step["evidence_ids"], [])

    def test_reconcile_is_idempotent(self) -> None:
        self.store.reserve_llm_call(self.investigation_id, "investigation", "fake")
        self.store.reconcile_interrupted(self.investigation_id)
        self.assertEqual(self.store.reconcile_interrupted(self.investigation_id), {"interrupted_calls": 0, "interrupted_steps": 0})

    def test_resume_after_interruption_continues_with_remaining_budget(self) -> None:
        # Simulate: call 1 reserved, step created, then the process was killed.
        self.store.reserve_llm_call(self.investigation_id, "investigation", "fake")
        self.store.add_step(self.investigation_id, revision=1, llm_call_number=1, action_type="github_tool", rationale="look",
                            question_to_answer="q", based_on_evidence_ids=[self.ev], tool_name="fake_tool", tool_arguments={})
        self.store.set_status(self.investigation_id, InvestigationStatus.PAUSED_EXTERNAL, error="killed")

        self.store.resume_after_external_pause(self.investigation_id)   # reconciles, then investigating
        steps = self.store.list_steps(self.investigation_id)
        self.assertEqual(steps[0]["result_status"], "error")

        verdict = {"decision": "adopt", "confidence": "low", "executive_summary": "ok",
                   "positive_signals": [{"statement": "exists", "evidence_ids": [self.ev], "claim_type": "observed"}],
                   "risk_factors": [], "adoption_conditions": [], "unverified_items": [], "decisive_evidence_ids": [self.ev]}
        llm = FakeLLM([
            LLMResponse([LLMToolCall("fake_tool", {"rationale": "r", "question_to_answer": "q", "based_on_evidence_ids": [self.ev]})], None, "id", 1, 1, {}),
            LLMResponse([LLMToolCall("submit_verdict", {"rationale": "done", "based_on_evidence_ids": [self.ev], "verdict": verdict})], None, "id", 1, 1, {}),
        ])
        agent = InvestigationAgent(self.store, llm, FakeTools(), ReportRenderer(self.store, self.settings))
        result = agent.run(self.investigation_id)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["investigation_calls_used"], 3, "interrupted call 1 still counts; resume used 2 and 3")
        self.assertEqual(result["remaining_budget"], 2)
        ordinals = [c["ordinal"] for c in self.store.list_llm_calls(self.investigation_id)]
        self.assertEqual(ordinals, [1, 2, 3])
        statuses = [c["status"] for c in self.store.list_llm_calls(self.investigation_id)]
        self.assertEqual(statuses, ["interrupted", "completed", "completed"])
        steps = self.store.list_steps(self.investigation_id)
        self.assertEqual([s["result_status"] for s in steps], ["error", "success", "success"])
        report = ReportRenderer(self.store, self.settings).render(self.investigation_id).read_text(encoding="utf-8")
        self.assertIn("Interrupted: execution stopped", report)
        self.assertNotIn("No result recorded", report)


if __name__ == "__main__":
    unittest.main()
