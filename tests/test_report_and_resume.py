from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from repo_detective.config import Settings
from repo_detective.report import ReportRenderer
from repo_detective.storage import InvestigationStore


class ReportAndResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = InvestigationStore(root / "test.db")
        self.settings = Settings(
            root, root / "test.db", root / "reports", None,
            "https://api.openai.com/v1", None, None,
            "https://api.github.com", "2026-03-10", 5, 60, 50, 1000,
        )
        self.settings.reports_dir.mkdir()
        self.investigation_id = self.store.create_investigation(
            input_url="owner/repo", owner="owner", repo="repo", goal="adopt?", initial_budget=3
        )
        self.evidence_id = self.store.add_evidence(
            self.investigation_id,
            step_id=None,
            source="intake",
            tool_name="get_repository",
            api_url="https://api.github.com/repos/owner/repo",
            html_url="https://github.com/owner/repo",
            request_parameters={},
            http_status=200,
            verification_status="verified",
            summary="Repository metadata was verified",
            normalized={"archived": False},
            rate_limit_remaining=59,
            raw={},
        )
        self.store.save_intake(
            self.investigation_id,
            {"canonical_full_name": "owner/repo", "stars": 1, "archived": False},
            canonical_full_name="owner/repo",
            html_url="https://github.com/owner/repo",
        )
        self.verdict = {
            "decision": "adopt",
            "confidence": "medium",
            "executive_summary": "Evidence supports adoption.",
            "positive_signals": [
                {"statement": "Repository metadata was verified", "evidence_ids": [self.evidence_id], "claim_type": "observed"}
            ],
            "risk_factors": [],
            "adoption_conditions": [],
            "unverified_items": [],
            "decisive_evidence_ids": [self.evidence_id],
        }
        self.store.complete_investigation(self.investigation_id, self.verdict)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_report_is_rendered_from_stored_data(self) -> None:
        path = ReportRenderer(self.store, self.settings).render(self.investigation_id)
        text = path.read_text(encoding="utf-8")
        self.assertIn("**ADOPT**", text)
        self.assertIn(self.evidence_id, text)
        self.assertIn("No LLM call was used", text)

    def test_retask_preserves_investigation_and_increments_revision(self) -> None:
        self.store.begin_retask(self.investigation_id, "Check the biggest fork")
        updated = self.store.get_investigation(self.investigation_id)
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["status"], "investigating")
        messages = self.store.list_chat_messages(self.investigation_id)
        self.assertEqual(messages[-1]["mode"], "retask")


if __name__ == "__main__":
    unittest.main()

