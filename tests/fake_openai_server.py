"""Scripted OpenAI-compatible /chat/completions server for end-to-end tests.

It behaves like a deterministic 'model': reads the JSON context the app sends,
picks the next tool by a fixed plan, and submits an evidence-cited verdict.
Scenario switches via env vars:
  FAKE_REJECT_REQUIRED=1  -> HTTP 400 mentioning tool_choice when 'required' is sent
  FAKE_PARALLEL=1         -> first turn returns two tool calls at once
  FAKE_PROSE=1            -> second turn returns prose without any tool call
  FAKE_REQUEST_BUDGET=1   -> third turn asks the human for 3 more calls
"""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PLAN = ["list_commits", "list_repository_advisories", "list_issues", "list_forks"]


def tool_call(name: str, arguments: dict, call_id: str = "call-1") -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def verdict(ctx: dict, ids: list[str], decision: str) -> dict:
    return {
        "decision": decision,
        "confidence": "medium",
        "executive_summary": f"Scripted verdict for {ctx['repository']} based on {len(ids)} evidence records.",
        "positive_signals": [{"statement": "Repository metadata was retrieved.", "evidence_ids": ids[:1], "claim_type": "observed"}]
        if decision != "reject" else [],
        "risk_factors": [{"statement": "Scripted risk citing collected evidence.", "evidence_ids": ids[-1:], "claim_type": "inference"}],
        "adoption_conditions": ["Pin a reviewed release"] if decision == "adopt_with_conditions" else [],
        "unverified_items": ["This verdict was produced by a scripted test model"],
        "decisive_evidence_ids": ids[:3],
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter
        sys.stderr.write("fake-llm: " + (fmt % args) + "\n")

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _reply(self, calls: list[dict] | None, content: str | None = None) -> None:
        self._send(200, {
            "id": "fake-1", "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content, "tool_calls": calls}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        })

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if os.getenv("FAKE_REJECT_REQUIRED") == "1" and payload.get("tool_choice") == "required":
            self._send(400, {"error": {"message": "tool_choice 'required' is not supported by this provider"}})
            return
        tools = [t["function"]["name"] for t in payload["tools"]]
        system = payload["messages"][0]["content"]
        user = payload["messages"][1]["content"]

        if system.startswith("You answer questions"):
            ctx = json.loads(user)
            q = ctx["user_question"].lower()
            if any(w in q for w in ("check", "investigate", "look at", "fork")):
                self._reply([tool_call("resume_investigation", {"instruction": ctx["user_question"], "reason": "New research requested by the tech lead"})])
            else:
                ids = [e["id"] for e in ctx["evidence"]][:2]
                v = ctx.get("verdict") or {}
                answer = f"Stored verdict is {v.get('decision')}; risk factors: " + "; ".join(r["statement"] for r in v.get("risk_factors", []))
                self._reply([tool_call("answer_from_log", {"answer": answer, "evidence_ids": ids})])
            return

        ctx = json.loads(user.split("\n\n", 1)[1])
        ids = [e["id"] for e in ctx["evidence_ledger"]]
        log = ctx["investigation_log"]
        done = [s["action"] for s in log if s["action"] in PLAN]
        common = {"rationale": "Scripted next step", "question_to_answer": "Scripted question", "based_on_evidence_ids": ids[:2]}
        step = len(log)

        if ctx["user_retasks"] and log and log[-1]["action"] == "submit_verdict":
            self._reply([tool_call("list_forks", {**common, "sort": "stargazers", "per_page": 5, "rationale": "Re-task: " + ctx["user_retasks"][-1]})])
            return
        if ctx["user_retasks"] and log and log[-1]["action"] == "list_forks":
            details = json.loads(next(e["details"] for e in ctx["evidence_ledger"] if e["tool"] == "list_forks"))
            top = details["forks"][0]["full_name"] if details.get("forks") else ctx["repository"]
            self._reply([tool_call("get_repository", {**common, "repository": top, "rationale": "Inspect the biggest fork"})])
            return

        if os.getenv("FAKE_PROSE") == "1" and step == 1:
            self._reply(None, "I think we should look at the commits next.")
            return
        if os.getenv("FAKE_REQUEST_BUDGET") == "1" and step == 2 and "request_more_budget" in tools and not any(s["action"] == "request_more_budget" for s in log):
            self._reply([tool_call("request_more_budget", {
                "rationale": "Need to inspect the advisory trail", "based_on_evidence_ids": ids[:2],
                "summary_so_far": "Commits and advisories were sampled.", "provisional_verdict": verdict(ctx, ids, "adopt_with_conditions"),
                "unresolved_questions": ["Was the suspicious release reverted?"], "proposed_next_checks": ["compare_refs", "get_commit"],
                "requested_calls": 3, "expected_verdict_impact": "Could move the verdict from conditional adoption to reject.",
            })])
            return

        nxt = [t for t in PLAN if t not in done and t in tools]
        if nxt:
            calls = [tool_call(nxt[0], {**common, "per_page": 10})]
            if os.getenv("FAKE_PARALLEL") == "1" and step == 0:
                calls.append(tool_call("list_issues", {**common, "per_page": 5}, "call-2"))
            self._reply(calls)
            return

        intake = ctx.get("intake") or {}
        decision = "reject" if intake.get("archived") else "adopt_with_conditions"
        if ctx["user_retasks"]:
            decision = "adopt"  # show the verdict can change after a re-task
        self._reply([tool_call("submit_verdict", {"rationale": "Scripted evidence is sufficient", "based_on_evidence_ids": ids[:2], "verdict": verdict(ctx, ids, decision)})])


if __name__ == "__main__":
    port = int(os.getenv("FAKE_PORT", "8089"))
    print(f"fake OpenAI server on 0.0.0.0:{port}", flush=True)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
