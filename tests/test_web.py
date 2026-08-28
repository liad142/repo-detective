from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from repo_detective.agent import InvestigationAgent
from repo_detective.chat import GroundedChatService
from repo_detective.config import Settings
from repo_detective.github import GitHubResponse, IntakeService
from repo_detective.models import LLMResponse, LLMToolCall, ToolResult, ToolResultStatus
from repo_detective.report import ReportRenderer
from repo_detective.storage import InvestigationStore
from repo_detective.web import WebApp, serve


class FakeGitHub:
    def get(self, endpoint, params=None):
        if endpoint == "/repos/owner/repo":
            body = {"id": 1, "full_name": "owner/repo", "html_url": "https://github.com/owner/repo", "stargazers_count": 5,
                    "forks_count": 1, "open_issues_count": 0, "archived": False, "default_branch": "main", "license": None,
                    "created_at": "2020-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z", "pushed_at": "2026-01-01T00:00:00Z", "topics": []}
            return GitHubResponse(200, body, {}, endpoint, endpoint)
        if endpoint.endswith("/releases/latest"):
            return GitHubResponse(404, {"message": "Not Found"}, {}, endpoint, endpoint)
        if endpoint.endswith("/contributors"):
            return GitHubResponse(200, [{"login": "alice", "contributions": 3}], {}, endpoint, endpoint)
        return GitHubResponse(404, {"message": "Not Found"}, {}, endpoint, endpoint)


class ScriptedLLM:
    """Investigation: one tool call, then a verdict. Chat: answer from log."""
    model = "fake"

    def choose_tool(self, *, messages, tools):
        names = [t["function"]["name"] for t in tools]
        context = messages[1]["content"]
        if "answer_from_log" in names:
            ctx = json.loads(context)
            return LLMResponse([LLMToolCall("answer_from_log", {"answer": "Stored verdict explained.", "evidence_ids": [ctx["evidence"][0]["id"]]})], None, "r", 1, 1, {})
        ctx = json.loads(context.split("\n\n", 1)[1])
        ids = [e["id"] for e in ctx["evidence_ledger"]]
        if not ctx["investigation_log"]:
            return LLMResponse([LLMToolCall("fake_tool", {"rationale": "look", "question_to_answer": "what?", "based_on_evidence_ids": ids[:1]})], None, "r", 1, 1, {})
        verdict = {"decision": "adopt", "confidence": "high", "executive_summary": "Fine.",
                   "positive_signals": [{"statement": "ok", "evidence_ids": ids[:1], "claim_type": "observed"}],
                   "risk_factors": [], "adoption_conditions": [], "unverified_items": [], "decisive_evidence_ids": ids[:1]}
        return LLMResponse([LLMToolCall("submit_verdict", {"rationale": "done", "based_on_evidence_ids": ids[:1], "verdict": verdict})], None, "r", 1, 1, {})


class FakeTools:
    definitions = [{"type": "function", "function": {"name": "fake_tool", "description": "f", "parameters": {"type": "object", "properties": {}}}}]
    names = {"fake_tool"}

    def __init__(self, store):
        self.store = store

    def execute(self, name, *, investigation, step_id, arguments):
        time.sleep(0.05)
        ev = self.store.add_evidence(investigation["id"], step_id=step_id, source="github_rest", tool_name="fake_tool",
                                     api_url="https://api.github.com/x", html_url=None, request_parameters={}, http_status=200,
                                     verification_status="verified", summary="Fake fact", normalized={"a": 1}, rate_limit_remaining=50, raw={})
        return ToolResult(ToolResultStatus.SUCCESS, "Fake fact was inspected", {"a": 1}, [ev])


class Runtime:
    def __init__(self, root: Path):
        self.settings = Settings(root, root / "t.db", root / "reports", "k", "https://x/v1", "m", None,
                                 "https://api.github.com", "2026-03-10", 5, 60, 50, 1000)
        self.settings.reports_dir.mkdir()
        self.store = InvestigationStore(self.settings.database_path)
        self.reports = ReportRenderer(self.store, self.settings)
        self.intake = IntakeService(FakeGitHub(), self.store)
        llm = ScriptedLLM()
        self.agent = InvestigationAgent(self.store, llm, FakeTools(self.store), self.reports)
        self.chat = GroundedChatService(self.store, llm, self.agent, self.reports)


class WebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Runtime(Path(self.temp.name))
        self.server = serve(WebApp(self.runtime), "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def call(self, method: str, path: str, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, (json.loads(data) if resp.getheader("Content-Type", "").startswith("application/json") else data)

    def wait_for(self, investigation_id: str, status: str, timeout: float = 5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            code, snap = self.call("GET", f"/api/investigations/{investigation_id}")
            if snap["investigation"]["status"] == status and not snap["busy"]:
                return snap
            time.sleep(0.05)
        raise AssertionError(f"did not reach {status}")

    def test_index_and_full_loop_over_http(self) -> None:
        code, page = self.call("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn(b"Repo Detective", page)

        code, body = self.call("POST", "/api/investigations", {"repository": "owner/repo"})
        self.assertEqual(code, 202)
        snap = self.wait_for(body["id"], "completed")
        self.assertEqual(snap["investigation"]["verdict"]["decision"], "adopt")
        self.assertEqual([s["action_type"] for s in snap["steps"]], ["github_tool", "submit_verdict"])
        self.assertEqual(len(snap["evidence"]), 4, "3 intake rows + 1 tool row, raw bodies excluded")
        self.assertNotIn("raw", snap["evidence"][0])
        self.assertTrue(snap["report_available"])
        self.assertEqual(snap["activity"]["kind"], "idle")

        code, report = self.call("GET", f"/api/investigations/{body['id']}/report")
        self.assertEqual(code, 200)
        self.assertIn(b"**ADOPT**", report)

        code, _ = self.call("POST", f"/api/investigations/{body['id']}/chat", {"message": "why?"})
        self.assertEqual(code, 202)
        deadline = time.time() + 5
        while time.time() < deadline:
            _, snap = self.call("GET", f"/api/investigations/{body['id']}")
            if any(m["mode"] == "answer" for m in snap["chat"]):
                break
            time.sleep(0.05)
        answers = [m for m in snap["chat"] if m["mode"] == "answer"]
        self.assertEqual(answers[0]["content"], "Stored verdict explained.")
        self.assertEqual(snap["investigation"]["chat_calls_used"], 1)
        self.assertEqual(snap["investigation"]["investigation_calls_used"], 2)

    def test_invalid_input_and_unknown_id_are_clean_errors(self) -> None:
        code, body = self.call("POST", "/api/investigations", {"repository": "https://evil.example/a/b"})
        self.assertEqual(code, 400)
        self.assertIn("github.com", body["error"])
        code, body = self.call("GET", "/api/investigations/nope")
        self.assertEqual(code, 404)
        code, body = self.call("POST", "/api/investigations/nope/approve", {"calls": 3})
        self.assertEqual(code, 404)

    def test_events_stream_emits_state(self) -> None:
        code, body = self.call("POST", "/api/investigations", {"repository": "owner/repo"})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", f"/api/investigations/{body['id']}/events")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.getheader("Content-Type", "").startswith("text/event-stream"))
        line = resp.readline()
        self.assertEqual(line, b"event: state\n")
        data = resp.readline()
        self.assertTrue(data.startswith(b"data: {"))
        conn.close()
        self.wait_for(body["id"], "completed")


if __name__ == "__main__":
    unittest.main()
