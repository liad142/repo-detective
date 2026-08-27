from __future__ import annotations

import io
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from repo_detective import github as github_module
from repo_detective.config import Settings
from repo_detective.github import (
    GitHubClient,
    GitHubResponse,
    IntakeService,
    parse_repository_input,
    response_verification_status,
)
from repo_detective.models import VerificationStatus
from repo_detective.storage import InvestigationStore


class ParseRepositoryTests(unittest.TestCase):
    def test_accepts_url_and_owner_repo(self) -> None:
        self.assertEqual(
            parse_repository_input("https://github.com/expressjs/express"),
            ("expressjs", "express"),
        )
        self.assertEqual(parse_repository_input("request/request.git"), ("request", "request"))

    def test_rejects_non_github_hosts(self) -> None:
        with self.assertRaisesRegex(ValueError, "github.com"):
            parse_repository_input("https://evil.example/owner/repo")


class CachePolicyTests(unittest.TestCase):
    """Rate limits must never be cached; a 403 would otherwise poison the whole TTL."""

    def make_client(self, root: Path) -> GitHubClient:
        store = InvestigationStore(root / "test.db")
        settings = Settings(
            root, root / "test.db", root / "reports", None, "https://api.openai.com/v1", None,
            None, "https://api.github.com", "2026-03-10", 5, 600, 50, 1000,
        )
        return GitHubClient(settings, store)

    def test_empty_string_filters_are_dropped_from_the_query(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = self.make_client(Path(directory))
            seen: list[str] = []

            def fake_urlopen(request, timeout=None):
                seen.append(request.full_url)
                return _Response(b"[]")

            with mock.patch.object(github_module.urllib.request, "urlopen", fake_urlopen):
                client.get("/repos/o/r/commits", {"author": "", "since": None, "page": 1, "per_page": 50})
            self.assertEqual(seen, ["https://api.github.com/repos/o/r/commits?page=1&per_page=50"])

    def test_rate_limited_response_is_not_cached_but_success_is(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = self.make_client(Path(directory))
            attempts = 0

            def fake_urlopen(request, timeout=None):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise urllib.error.HTTPError(
                        request.full_url, 403, "Forbidden",
                        {"content-type": "application/json", "x-ratelimit-remaining": "0"},
                        io.BytesIO(b'{"message":"API rate limit exceeded"}'),
                    )
                return _Response(b'{"full_name":"owner/repo"}')

            with mock.patch.object(github_module.urllib.request, "urlopen", fake_urlopen):
                first = client.get("/repos/owner/repo")
                second = client.get("/repos/owner/repo")
                third = client.get("/repos/owner/repo")

            self.assertEqual(response_verification_status(first), VerificationStatus.RATE_LIMITED)
            self.assertFalse(first.from_cache)
            self.assertEqual(second.status, 200)
            self.assertFalse(second.from_cache, "the 403 must not have been served from cache")
            self.assertTrue(third.from_cache, "the 200 is cached within the TTL")
            self.assertEqual(attempts, 2)


class _Response(io.BytesIO):
    status = 200

    def __init__(self, body: bytes):
        super().__init__(body)
        self.headers = {"Content-Type": "application/json", "ETag": '"abc"'}

    def geturl(self) -> str:
        return "https://api.github.com/repos/owner/repo"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class FakeGitHubClient:
    def get(self, endpoint: str, params=None):
        if endpoint == "/repos/owner/repo":
            body = {
                "id": 1,
                "full_name": "canonical/repo",
                "html_url": "https://github.com/canonical/repo",
                "description": "demo",
                "stargazers_count": 10,
                "forks_count": 2,
                "open_issues_count": 1,
                "archived": False,
                "disabled": False,
                "fork": False,
                "default_branch": "main",
                "license": {"spdx_id": "MIT"},
                "created_at": "2020-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "pushed_at": "2026-01-01T00:00:00Z",
                "topics": [],
            }
            return GitHubResponse(200, body, {"x-ratelimit-remaining": "59"}, endpoint, endpoint)
        if endpoint.endswith("/releases/latest"):
            return GitHubResponse(404, {"message": "Not Found"}, {}, endpoint, endpoint)
        if endpoint.endswith("/contributors"):
            body = [{"login": "alice", "contributions": 9}, {"login": "bob", "contributions": 1}]
            return GitHubResponse(200, body, {}, endpoint, endpoint)
        raise AssertionError(f"Unexpected endpoint: {endpoint}")


class IntakeTests(unittest.TestCase):
    def test_intake_is_deterministic_and_tolerates_no_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = InvestigationStore(Path(directory) / "test.db")
            investigation_id = store.create_investigation(
                input_url="owner/repo", owner="owner", repo="repo", goal="adopt?"
            )
            intake = IntakeService(FakeGitHubClient(), store).run(investigation_id)
            self.assertEqual(intake["canonical_full_name"], "canonical/repo")
            self.assertIsNone(intake["latest_release"])
            self.assertEqual(len(store.list_evidence(investigation_id)), 3)
            self.assertEqual(store.get_investigation(investigation_id)["status"], "investigating")


if __name__ == "__main__":
    unittest.main()

