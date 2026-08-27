from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from repo_detective.models import BudgetExhausted, InvestigationStatus
from repo_detective.storage import InvestigationStore


class StorageBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = InvestigationStore(Path(self.temp.name) / "test.db")
        self.investigation_id = self.store.create_investigation(
            input_url="owner/repo",
            owner="owner",
            repo="repo",
            goal="Should we adopt?",
            initial_budget=2,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_hard_budget_reservation(self) -> None:
        self.store.reserve_llm_call(self.investigation_id, "investigation", "fake")
        self.store.reserve_llm_call(self.investigation_id, "investigation", "fake")
        with self.assertRaises(BudgetExhausted):
            self.store.reserve_llm_call(self.investigation_id, "investigation", "fake")

    def test_chat_calls_do_not_consume_investigation_budget(self) -> None:
        self.store.reserve_llm_call(self.investigation_id, "chat", "fake")
        item = self.store.get_investigation(self.investigation_id)
        self.assertEqual(item["remaining_budget"], 2)
        self.assertEqual(item["chat_calls_used"], 1)

    def test_resume_only_from_external_pause_with_budget(self) -> None:
        with self.assertRaisesRegex(ValueError, "status is created"):
            self.store.resume_after_external_pause(self.investigation_id)
        self.store.set_status(self.investigation_id, InvestigationStatus.PAUSED_EXTERNAL, error="boom")
        self.store.resume_after_external_pause(self.investigation_id)
        self.assertEqual(self.store.get_investigation(self.investigation_id)["status"], "investigating")

        self.store.reserve_llm_call(self.investigation_id, "investigation", "fake")
        self.store.reserve_llm_call(self.investigation_id, "investigation", "fake")
        self.store.set_status(self.investigation_id, InvestigationStatus.PAUSED_EXTERNAL, error="boom")
        with self.assertRaises(BudgetExhausted):
            self.store.resume_after_external_pause(self.investigation_id)

    def test_budget_approval_requires_waiting_state(self) -> None:
        with self.assertRaisesRegex(ValueError, "awaiting budget"):
            self.store.approve_budget(self.investigation_id, 3, "test")


if __name__ == "__main__":
    unittest.main()

