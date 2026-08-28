from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from repo_detective import osv as osv_module
from repo_detective.config import Settings
from repo_detective.storage import InvestigationStore
from repo_detective.tools import GitHubToolRegistry


class _Response(io.BytesIO):
    status = 200

    def __init__(self, body: bytes):
        super().__init__(body)
        self.headers = {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


OSV_BODY = {
    "vulns": [
        {
            "id": "GHSA-xxxx-yyyy-zzzz", "aliases": ["CVE-2025-0001"], "summary": "Malicious code in chalk",
            "published": "2025-09-08T00:00:00Z", "modified": "2025-09-09T00:00:00Z",
            "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:U/C:H/I:H/A:H"}],
            "affected": [{"package": {"name": "chalk", "ecosystem": "npm"}, "ranges": [{"type": "SEMVER", "events": [{"introduced": "5.6.1"}, {"fixed": "5.6.2"}]}], "versions": ["5.6.1"]}],
            "references": [{"type": "WEB", "url": "https://github.com/chalk/chalk/issues/656"}],
        }
    ]
}


class QueryOSVTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = InvestigationStore(root / "test.db")
        settings = Settings(root, root / "test.db", root / "reports", None, "https://api.openai.com/v1", None,
                            None, "https://api.github.com", "2026-03-10", 5, 60, 50, 1000)
        self.tools = GitHubToolRegistry(client=None, store=self.store, settings=settings)
        self.investigation_id = self.store.create_investigation(input_url="owner/repo", owner="owner", repo="repo", goal="adopt?")
        self.investigation = self.store.get_investigation(self.investigation_id)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_tool(self, args):
        step_id = self.store.add_step(self.investigation_id, revision=1, llm_call_number=1, action_type="github_tool",
                                      rationale="r", question_to_answer="q", based_on_evidence_ids=[], tool_name="query_osv", tool_arguments=args)
        return self.tools.execute("query_osv", investigation=self.investigation, step_id=step_id,
                                  arguments={"rationale": "r", "question_to_answer": "q", "based_on_evidence_ids": [], **args})

    def test_query_posts_mapped_ecosystem_and_records_evidence(self) -> None:
        sent = {}

        def fake_urlopen(request, timeout=None):
            sent["url"] = request.full_url
            sent["payload"] = json.loads(request.data.decode("utf-8"))
            return _Response(json.dumps(OSV_BODY).encode())

        with mock.patch.object(osv_module.urllib.request, "urlopen", fake_urlopen):
            result = self.run_tool({"ecosystem": "npm", "package": "chalk", "version": "5.6.1"})

        self.assertEqual(sent["url"], "https://api.osv.dev/v1/query")
        self.assertEqual(sent["payload"], {"package": {"name": "chalk", "ecosystem": "npm"}, "version": "5.6.1"})
        self.assertEqual(result.status.value, "success")
        self.assertIn("OSV lists 1 vulnerabilities for npm:chalk@5.6.1", result.summary)
        vuln = result.facts["vulnerabilities"][0]
        self.assertEqual(vuln["aliases"], ["CVE-2025-0001"])
        self.assertEqual(vuln["affected"][0]["events"], [{"introduced": "5.6.1"}, {"fixed": "5.6.2"}])
        evidence = self.store.list_evidence(self.investigation_id)
        self.assertEqual(evidence[-1]["tool_name"], "query_osv")
        self.assertEqual(evidence[-1]["verification_status"], "verified")
        self.assertEqual(evidence[-1]["raw"], OSV_BODY)

    def test_pip_maps_to_pypi_and_missing_version_is_flagged(self) -> None:
        sent = {}

        def fake_urlopen(request, timeout=None):
            sent["payload"] = json.loads(request.data.decode("utf-8"))
            return _Response(b'{"vulns": []}')

        with mock.patch.object(osv_module.urllib.request, "urlopen", fake_urlopen):
            result = self.run_tool({"ecosystem": "pip", "package": "requests"})
        self.assertEqual(sent["payload"], {"package": {"name": "requests", "ecosystem": "PyPI"}})
        self.assertTrue(any("No version" in item for item in result.limitations))

    def test_network_failure_is_unavailable_not_absence(self) -> None:
        def fake_urlopen(request, timeout=None):
            raise urllib.error.URLError("dns down")

        with mock.patch.object(osv_module.urllib.request, "urlopen", fake_urlopen):
            result = self.run_tool({"ecosystem": "npm", "package": "chalk"})
        self.assertEqual(result.status.value, "unavailable")
        self.assertIn("could not be verified", result.summary)
        self.assertEqual(self.store.list_evidence(self.investigation_id)[-1]["verification_status"], "unavailable")

    def test_unknown_ecosystem_is_rejected_without_a_request(self) -> None:
        with mock.patch.object(osv_module.urllib.request, "urlopen", side_effect=AssertionError("must not be called")):
            result = self.run_tool({"ecosystem": "other", "package": "x"})
        self.assertEqual(result.status.value, "error")


if __name__ == "__main__":
    unittest.main()
