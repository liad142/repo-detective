from __future__ import annotations

import io
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from repo_detective.agent import InvestigationAgent
from repo_detective.config import Settings
from repo_detective.models import LLMResponse, LLMToolCall, ToolResult, ToolResultStatus
from repo_detective.report import ReportRenderer
from repo_detective.storage import InvestigationStore


class FakeLLM:
    model = "fake-model"

    def __init__(self, responses):
        self.responses = list(responses)
        self.tool_sets = []

    def choose_tool(self, *, messages, tools):
        self.tool_sets.append([item["function"]["name"] for item in tools])
        return self.responses.pop(0)


class FakeTools:
    definitions = [
        {
            "type": "function",
            "function": {
                "name": "fake_tool",
                "description": "fake",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    names = {"fake_tool"}

    def execute(self, name, *, investigation, step_id, arguments):
        return ToolResult(ToolResultStatus.SUCCESS, "Fake fact was inspected")


def response(name: str | None, arguments=None) -> LLMResponse:
    calls = [] if name is None else [LLMToolCall(name, arguments or {})]
    return LLMResponse(calls, None, "request-id", 10, 5, {})


class AgentLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = InvestigationStore(root / "test.db")
        self.settings = Settings(
            data_dir=root,
            database_path=root / "test.db",
            reports_dir=root / "reports",
            openai_api_key="fake",
            openai_base_url="https://example.test/v1",
            openai_model="fake-model",
            github_token=None,
            github_api_url="https://api.github.com",
            github_api_version="2026-03-10",
            http_timeout_seconds=5,
            github_cache_ttl_seconds=60,
            max_tool_items=50,
            max_file_chars=1000,
        )
        self.settings.reports_dir.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def create_ready_investigation(self, budget: int) -> tuple[str, str]:
        investigation_id = self.store.create_investigation(
            input_url="owner/repo", owner="owner", repo="repo", goal="adopt?", initial_budget=budget
        )
        evidence_id = self.store.add_evidence(
            investigation_id,
            step_id=None,
            source="intake",
            tool_name="get_repository",
            api_url="https://api.github.com/repos/owner/repo",
            html_url="https://github.com/owner/repo",
            request_parameters={},
            http_status=200,
            verification_status="verified",
            summary="Repository exists",
            normalized={"archived": False},
            rate_limit_remaining=59,
            raw={},
        )
        self.store.save_intake(
            investigation_id,
            {"canonical_full_name": "owner/repo", "evidence_ids": [evidence_id]},
            canonical_full_name="owner/repo",
            html_url="https://github.com/owner/repo",
        )
        return investigation_id, evidence_id

    def test_last_call_exposes_only_control_tools(self) -> None:
        investigation_id, ev = self.create_ready_investigation(2)
        verdict = {
            "decision": "adopt",
            "confidence": "medium",
            "executive_summary": "The limited evidence supports adoption.",
            "positive_signals": [{"statement": "Repository exists", "evidence_ids": [ev], "claim_type": "observed"}],
            "risk_factors": [],
            "adoption_conditions": [],
            "unverified_items": ["Only a test evidence set was used"],
            "decisive_evidence_ids": [ev],
        }
        fake_llm = FakeLLM(
            [
                response("fake_tool", {"rationale": "Inspect a lead", "question_to_answer": "What happened?", "based_on_evidence_ids": [ev]}),
                response("submit_verdict", {"rationale": "Evidence is sufficient", "based_on_evidence_ids": [ev], "verdict": verdict}),
            ]
        )
        agent = InvestigationAgent(self.store, fake_llm, FakeTools(), ReportRenderer(self.store, self.settings))
        result = agent.run(investigation_id)
        self.assertEqual(result["status"], "completed")
        self.assertIn("fake_tool", fake_llm.tool_sets[0])
        self.assertEqual(set(fake_llm.tool_sets[1]), {"submit_verdict", "request_more_budget"})
        self.assertEqual(result["investigation_calls_used"], 2)

    def test_parallel_tool_calls_execute_first_without_wasting_budget(self) -> None:
        investigation_id, ev = self.create_ready_investigation(3)
        common = {"rationale": "Inspect", "question_to_answer": "What?", "based_on_evidence_ids": [ev]}
        verdict = {
            "decision": "reject",
            "confidence": "low",
            "executive_summary": "Test reject.",
            "positive_signals": [],
            "risk_factors": [{"statement": "Risk", "evidence_ids": [ev], "claim_type": "observed"}],
            "adoption_conditions": [],
            "unverified_items": [],
            "decisive_evidence_ids": [ev],
        }
        two_calls = LLMResponse(
            [LLMToolCall("fake_tool", common), LLMToolCall("fake_tool", common)], None, "id", 1, 1, {}
        )
        fake_llm = FakeLLM(
            [two_calls, response("submit_verdict", {"rationale": "Done", "based_on_evidence_ids": [ev], "verdict": verdict})]
        )
        agent = InvestigationAgent(self.store, fake_llm, FakeTools(), ReportRenderer(self.store, self.settings))
        with unittest.mock.patch("sys.stderr", new=io.StringIO()):
            result = agent.run(investigation_id)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["investigation_calls_used"], 2)
        steps = self.store.list_steps(investigation_id)
        self.assertEqual([s["action_type"] for s in steps], ["github_tool", "submit_verdict"])

    def test_invalid_actions_never_overrun_budget(self) -> None:
        investigation_id, _ = self.create_ready_investigation(2)
        fake_llm = FakeLLM([response(None), response(None)])
        agent = InvestigationAgent(self.store, fake_llm, FakeTools(), ReportRenderer(self.store, self.settings))
        result = agent.run(investigation_id)
        self.assertEqual(result["investigation_calls_used"], 2)
        self.assertEqual(result["status"], "awaiting_budget")
        self.assertEqual(len(fake_llm.tool_sets), 2)


if __name__ == "__main__":
    unittest.main()

