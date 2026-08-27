from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from repo_detective.chat import GroundedChatService
from repo_detective.config import Settings
from repo_detective.models import LLMResponse, LLMToolCall
from repo_detective.report import ReportRenderer
from repo_detective.storage import InvestigationStore


class ResumeLLM:
    model = "fake"

    def __init__(self):
        self.messages = []

    def choose_tool(self, *, messages, tools):
        self.messages.append(messages)
        return LLMResponse(
            [LLMToolCall("resume_investigation", {"instruction": "check the biggest fork", "reason": "fork may be successor"})],
            None, "req", 1, 1, {},
        )


class AnswerLLM(ResumeLLM):
    def choose_tool(self, *, messages, tools):
        self.messages.append(messages)
        return LLMResponse(
            [LLMToolCall("answer_from_log", {"answer": "The investigation did not check that.", "evidence_ids": []})],
            None, "req", 1, 1, {},
        )


class RecordingAgent:
    def __init__(self, store):
        self.store = store
        self.runs = []

    def run(self, investigation_id):
        investigation = self.store.get_investigation(investigation_id)
        self.runs.append(investigation["status"])
        return investigation


class ChatResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = InvestigationStore(root / "test.db")
        self.settings = Settings(
            root, root / "test.db", root / "reports", "fake", "https://example.test/v1", "fake",
            None, "https://api.github.com", "2026-03-10", 5, 60, 50, 1000,
        )
        self.settings.reports_dir.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def completed_investigation(self, budget: int, used: int) -> str:
        investigation_id = self.store.create_investigation(
            input_url="owner/repo", owner="owner", repo="repo", goal="adopt?", initial_budget=budget
        )
        ev = self.store.add_evidence(
            investigation_id, step_id=None, source="intake", tool_name="get_repository",
            api_url="https://api.github.com/repos/owner/repo", html_url="https://github.com/owner/repo",
            request_parameters={}, http_status=200, verification_status="verified",
            summary="Repository exists", normalized={}, rate_limit_remaining=59, raw={},
        )
        self.store.save_intake(
            investigation_id, {"canonical_full_name": "owner/repo", "evidence_ids": [ev]},
            canonical_full_name="owner/repo", html_url="https://github.com/owner/repo",
        )
        for _ in range(used):
            self.store.reserve_llm_call(investigation_id, "investigation", "fake")
        self.store.complete_investigation(
            investigation_id,
            {
                "decision": "adopt", "confidence": "medium", "executive_summary": "ok",
                "positive_signals": [{"statement": "exists", "evidence_ids": [ev], "claim_type": "observed"}],
                "risk_factors": [], "adoption_conditions": [], "unverified_items": [],
                "decisive_evidence_ids": [ev],
            },
        )
        return investigation_id

    def test_retask_with_budget_resumes_agent_as_new_revision(self) -> None:
        investigation_id = self.completed_investigation(budget=5, used=2)
        agent = RecordingAgent(self.store)
        service = GroundedChatService(self.store, ResumeLLM(), agent, ReportRenderer(self.store, self.settings))
        outcome = service.ask(investigation_id, "now check the biggest fork")
        self.assertEqual(outcome["mode"], "retask")
        self.assertEqual(agent.runs, ["investigating"])
        updated = self.store.get_investigation(investigation_id)
        self.assertEqual(updated["revision"], 2)
        retasks = [m for m in self.store.list_chat_messages(investigation_id) if m["mode"] == "retask"]
        self.assertEqual([m["content"] for m in retasks], ["check the biggest fork"])

    def test_retask_after_exhausted_budget_pauses_and_keeps_instruction(self) -> None:
        investigation_id = self.completed_investigation(budget=2, used=2)
        agent = RecordingAgent(self.store)
        service = GroundedChatService(self.store, ResumeLLM(), agent, ReportRenderer(self.store, self.settings))
        outcome = service.ask(investigation_id, "now check the biggest fork")
        self.assertEqual(outcome["mode"], "budget_required")
        self.assertEqual(agent.runs, [], "no agent run without approved budget")
        paused = self.store.get_investigation(investigation_id)
        self.assertEqual(paused["status"], "awaiting_budget")
        self.assertEqual(paused["revision"], 2)
        self.assertEqual(paused["pending_budget_request"]["requested_calls"], 5)
        self.assertEqual(paused["verdict"]["decision"], "adopt", "prior verdict kept as provisional")

        # A human approval must hand the stored instruction to the resumed agent.
        self.store.approve_budget(investigation_id, 5, "test")
        approved = self.store.get_investigation(investigation_id)
        self.assertEqual(approved["status"], "investigating")
        self.assertEqual(approved["remaining_budget"], 5)
        retasks = [m for m in self.store.list_chat_messages(investigation_id) if m["mode"] == "retask" and m["role"] == "user"]
        self.assertEqual([m["content"] for m in retasks], ["check the biggest fork"])

    def test_unsupported_answer_phrasing_is_accepted_and_history_is_sent(self) -> None:
        investigation_id = self.completed_investigation(budget=5, used=1)
        llm = AnswerLLM()
        service = GroundedChatService(self.store, llm, RecordingAgent(self.store), ReportRenderer(self.store, self.settings))
        first = service.ask(investigation_id, "Did you check CI?")
        self.assertEqual(first["answer"], "The investigation did not check that.")
        second = service.ask(investigation_id, "And the license?")
        self.assertEqual(second["mode"], "answer")
        context = json.loads(llm.messages[-1][1]["content"])
        self.assertEqual(
            [(m["role"], m["content"]) for m in context["recent_chat"]],
            [("user", "Did you check CI?"), ("assistant", "The investigation did not check that.")],
        )
        self.assertEqual(context["user_question"], "And the license?")


if __name__ == "__main__":
    unittest.main()
