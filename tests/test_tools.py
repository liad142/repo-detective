from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from repo_detective.config import Settings
from repo_detective.github import GitHubResponse
from repo_detective.storage import InvestigationStore
from repo_detective.tools import GitHubToolRegistry


class FakeClient:
    def __init__(self):
        self.urls = []

    def get(self, endpoint, params=None):
        self.urls.append(endpoint)
        if endpoint.endswith("/contents/"):
            body = [
                {"name": "archify", "type": "dir", "size": 0},
                {"name": "README.md", "type": "file", "size": 18314},
            ]
            return GitHubResponse(200, body, {}, endpoint, endpoint)
        if endpoint.endswith("/contents/archify"):
            body = [{"name": "package.json", "type": "file", "size": 1382}]
            return GitHubResponse(200, body, {}, endpoint, endpoint)
        return GitHubResponse(404, {"message": "Not Found"}, {}, endpoint, endpoint)


class ReadRepositoryFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = InvestigationStore(root / "test.db")
        settings = Settings(
            root, root / "test.db", root / "reports", None, "https://api.openai.com/v1", None,
            None, "https://api.github.com", "2026-03-10", 5, 60, 50, 1000,
        )
        self.client = FakeClient()
        self.tools = GitHubToolRegistry(self.client, self.store, settings)
        self.investigation_id = self.store.create_investigation(
            input_url="owner/repo", owner="owner", repo="repo", goal="adopt?"
        )
        self.investigation = self.store.get_investigation(self.investigation_id)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_tool(self, path: str):
        step_id = self.store.add_step(
            self.investigation_id, revision=1, llm_call_number=1, action_type="github_tool",
            rationale="r", question_to_answer="q", based_on_evidence_ids=[], tool_name="read_repository_file",
            tool_arguments={"path": path},
        )
        return self.tools.execute(
            "read_repository_file", investigation=self.investigation, step_id=step_id,
            arguments={"rationale": "r", "question_to_answer": "q", "based_on_evidence_ids": [], "path": path},
        )

    def test_empty_path_lists_repository_root(self) -> None:
        result = self.run_tool("")
        self.assertEqual(result.status.value, "success")
        self.assertEqual([e["name"] for e in result.facts["entries"]], ["archify", "README.md"])
        self.assertEqual(self.client.urls[-1], "/repos/owner/repo/contents/")
        self.assertEqual(len(result.evidence_ids), 1)

    def test_directory_path_lists_entries_and_not_found_stays_explicit(self) -> None:
        listing = self.run_tool("archify")
        self.assertEqual(listing.facts["entries"][0]["name"], "package.json")
        missing = self.run_tool("package.json")
        self.assertEqual(missing.status.value, "not_found")
        self.assertIn("could not be verified", missing.summary)

    def test_traversal_is_rejected(self) -> None:
        result = self.run_tool("../etc/passwd")
        self.assertEqual(result.status.value, "error")
        self.assertNotIn("/repos/owner/repo/contents/..", "".join(self.client.urls))


if __name__ == "__main__":
    unittest.main()
