from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from repo_detective.chat import GroundedChatService
from repo_detective.config import Settings
from repo_detective.models import LLMResponse, LLMToolCall
from repo_detective.report import ReportRenderer
from repo_detective.storage import InvestigationStore


class FakeChatLLM:
    model = "fake"

    def __init__(self, evidence_id):
        self.evidence_id = evidence_id
        self.tool_names = []

    def choose_tool(self, *, messages, tools):
        self.tool_names = [item["function"]["name"] for item in tools]
        return LLMResponse(
            [LLMToolCall("answer_from_log", {"answer": "It is supported by stored evidence.", "evidence_ids": [self.evidence_id]})],
            None,
            "request",
            10,
            5,
            {},
        )


class NeverCalledAgent:
    def run(self, investigation_id):
        raise AssertionError("Agent should not run for a grounded answer")


class GroundedChatTests(unittest.TestCase):
    def test_question_has_no_github_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = InvestigationStore(root / "test.db")
            investigation_id = store.create_investigation(
                input_url="owner/repo", owner="owner", repo="repo", goal="adopt?"
            )
            evidence_id = store.add_evidence(
                investigation_id,
                step_id=None,
                source="intake",
                tool_name="get_repository",
                api_url="https://api.github.com/repos/owner/repo",
                html_url="https://github.com/owner/repo",
                request_parameters={},
                http_status=200,
                verification_status="verified",
                summary="Stored fact",
                normalized={},
                rate_limit_remaining=59,
                raw={},
            )
            settings = Settings(
                root, root / "test.db", root / "reports", "fake", "https://example.test/v1", "fake",
                None, "https://api.github.com", "2026-03-10", 5, 60, 50, 1000,
            )
            settings.reports_dir.mkdir()
            llm = FakeChatLLM(evidence_id)
            service = GroundedChatService(store, llm, NeverCalledAgent(), ReportRenderer(store, settings))
            outcome = service.ask(investigation_id, "Why?")
            self.assertEqual(outcome["mode"], "answer")
            self.assertEqual(set(llm.tool_names), {"answer_from_log", "resume_investigation"})
            self.assertEqual(store.get_investigation(investigation_id)["investigation_calls_used"], 0)
            self.assertEqual(store.get_investigation(investigation_id)["chat_calls_used"], 1)


if __name__ == "__main__":
    unittest.main()

