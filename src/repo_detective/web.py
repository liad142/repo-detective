from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .config import Settings
from .github import parse_repository_input
from .models import BudgetExhausted, InvestigationStatus, json_dumps
from .storage import InvestigationStore

STATIC_DIR = Path(__file__).parent / "static"
SNAPSHOT_DETAIL_CHARS = 4_000


class WebApp:
    """Thin HTTP adapter over the same services the CLI uses.

    Long-running work (intake + agent, chat re-task, approval) runs in a
    background thread per investigation; the browser follows progress through a
    Server-Sent Events stream that is derived from SQLite, so the UI shows
    exactly what the log records and nothing more.
    """

    def __init__(self, runtime: Any):
        self.runtime = runtime
        self.store: InvestigationStore = runtime.store
        self.settings: Settings = runtime.settings
        self._busy: set[str] = set()
        self._lock = threading.Lock()

    # ---- background work -------------------------------------------------

    def _spawn(self, investigation_id: str, work: Callable[[], None]) -> bool:
        with self._lock:
            if investigation_id in self._busy:
                return False
            self._busy.add(investigation_id)

        def runner() -> None:
            try:
                work()
            except Exception as exc:  # the store already holds the failure state
                print(f"web worker error [{investigation_id}]: {exc}", file=sys.stderr)
            finally:
                with self._lock:
                    self._busy.discard(investigation_id)

        threading.Thread(target=runner, daemon=True).start()
        return True

    def start_investigation(self, repository: str, budget: int = 30) -> str:
        owner, repo = parse_repository_input(repository)
        investigation_id = self.store.create_investigation(
            input_url=repository, owner=owner, repo=repo,
            goal="Should our engineering team adopt this open-source project?",
            initial_budget=budget,
        )

        def work() -> None:
            try:
                self.runtime.intake.run(investigation_id)
            except Exception:
                self.runtime.reports.render(investigation_id)
                return
            self.runtime.agent.run(investigation_id)
            self.runtime.reports.render(investigation_id)

        self._spawn(investigation_id, work)
        return investigation_id

    def chat(self, investigation_id: str, message: str) -> None:
        self.store.get_investigation(investigation_id)  # raises KeyError if unknown
        if not message.strip():
            raise ValueError("Chat message must not be empty")
        if not self._spawn(investigation_id, lambda: self.runtime.chat.ask(investigation_id, message)):
            raise ValueError("The agent is still working on this investigation")

    def approve(self, investigation_id: str, calls: int) -> None:
        self.store.approve_budget(investigation_id, calls, "Approved by human in web UI")
        self._spawn(investigation_id, lambda: (self.runtime.agent.run(investigation_id), self.runtime.reports.render(investigation_id)))

    def finalize(self, investigation_id: str) -> None:
        self.store.finalize_provisional(investigation_id)
        self.runtime.reports.render(investigation_id)

    def resume(self, investigation_id: str) -> None:
        if investigation_id in self._busy:
            raise ValueError("The agent is still working on this investigation")
        self.store.resume_after_external_pause(investigation_id)  # reconciles interrupted calls/steps first
        self._spawn(investigation_id, lambda: (self.runtime.agent.run(investigation_id), self.runtime.reports.render(investigation_id)))

    # ---- read model --------------------------------------------------------

    def snapshot(self, investigation_id: str) -> dict[str, Any]:
        investigation = self.store.get_investigation(investigation_id)
        steps = self.store.list_steps(investigation_id)
        calls = self.store.list_llm_calls(investigation_id)
        evidence = []
        rate_limit_remaining = None
        for item in self.store.list_evidence(investigation_id):
            details = json_dumps(item.get("normalized"))
            if item.get("rate_limit_remaining") is not None:
                rate_limit_remaining = item["rate_limit_remaining"]  # list is ordered by retrieved_at
            evidence.append({
                "id": item["id"], "tool": item["tool_name"], "status": item["verification_status"],
                "summary": item["summary"], "source_url": item.get("html_url") or item.get("api_url"),
                "http_status": item["http_status"], "retrieved_at": item["retrieved_at"],
                "details": details if len(details) <= SNAPSHOT_DETAIL_CHARS else details[:SNAPSHOT_DETAIL_CHARS] + "…",
            })
        chat = [
            {"role": m["role"], "mode": m["mode"], "content": m["content"], "evidence_ids": m["evidence_ids"], "created_at": m["created_at"]}
            for m in self.store.list_chat_messages(investigation_id)
        ]
        report = self.settings.reports_dir / f"{investigation_id}-r{investigation['revision']}.md"
        return {
            "investigation": {
                k: investigation.get(k) for k in (
                    "id", "input_url", "owner", "repo", "canonical_full_name", "html_url", "status",
                    "revision", "initial_budget", "approved_extra_budget", "investigation_calls_used",
                    "chat_calls_used", "remaining_budget", "intake", "verdict", "pending_budget_request",
                    "last_error", "created_at", "updated_at",
                )
            },
            "activity": self._activity(investigation, steps, calls),
            "github_rate_limit_remaining": rate_limit_remaining,
            "steps": [
                {k: s.get(k) for k in (
                    "sequence", "revision", "llm_call_number", "action_type", "rationale", "question_to_answer",
                    "based_on_evidence_ids", "tool_name", "tool_arguments", "result_status", "observation",
                    "evidence_ids", "created_at", "completed_at",
                )} for s in steps
            ],
            "evidence": evidence,
            "chat": chat,
            "busy": investigation_id in self._busy,
            "report_available": report.exists(),
        }

    @staticmethod
    def _activity(investigation: dict[str, Any], steps: list[dict[str, Any]], calls: list[dict[str, Any]]) -> dict[str, Any]:
        status = investigation["status"]
        last_call = calls[-1] if calls else None
        if status == InvestigationStatus.INTAKE_RUNNING.value:
            return {"kind": "intake", "label": "Collecting starting facts from GitHub"}
        if last_call and last_call["status"] == "reserved":
            if last_call["purpose"] == "chat":
                return {"kind": "answering", "label": "Reading the stored log"}
            return {"kind": "thinking", "label": f"Choosing the next action (call {last_call['ordinal']})", "call": last_call["ordinal"]}
        if status == InvestigationStatus.INVESTIGATING.value and steps and not steps[-1].get("completed_at"):
            return {"kind": "checking", "label": f"Checking {steps[-1].get('tool_name') or steps[-1]['action_type']}", "tool": steps[-1].get("tool_name")}
        return {"kind": "idle", "label": status.replace("_", " ")}

    def list_investigations(self) -> list[dict[str, Any]]:
        return self.store.list_investigations()

    def report_text(self, investigation_id: str) -> str:
        return self.runtime.reports.render(investigation_id).read_text(encoding="utf-8")


def make_handler(app: WebApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "repo-detective-web/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet by default
            if "/events" not in self.path:
                sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        # -- helpers
        def _json(self, status: int, payload: Any) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 64_000:
                raise ValueError("Request body too large")
            raw = self.rfile.read(length) if length else b""
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(payload, dict):
                raise ValueError("JSON object expected")
            return payload

        def _route(self) -> tuple[str, list[str]]:
            path = urlsplit(self.path).path
            return path, [p for p in path.split("/") if p]

        def _resolve(self, value: str) -> str:
            return app.store.latest_investigation_id() if value == "latest" else value

        # -- GET
        def do_GET(self) -> None:
            path, parts = self._route()
            try:
                if path == "/" or path == "/index.html":
                    return self._static("index.html", "text/html; charset=utf-8")
                if parts[:2] == ["api", "investigations"] and len(parts) == 2:
                    return self._json(200, {"investigations": app.list_investigations()})
                if parts[:2] == ["api", "investigations"] and len(parts) == 3:
                    return self._json(200, app.snapshot(self._resolve(parts[2])))
                if parts[:2] == ["api", "investigations"] and len(parts) == 4 and parts[3] == "events":
                    return self._events(self._resolve(parts[2]))
                if parts[:2] == ["api", "investigations"] and len(parts) == 4 and parts[3] == "report":
                    text = app.report_text(self._resolve(parts[2])).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/markdown; charset=utf-8")
                    self.send_header("Content-Length", str(len(text)))
                    self.end_headers()
                    self.wfile.write(text)
                    return
                self._json(404, {"error": "Not found"})
            except KeyError as exc:
                self._json(404, {"error": str(exc)})
            except (ValueError, BudgetExhausted) as exc:
                self._json(400, {"error": str(exc)})

        def _static(self, name: str, content_type: str) -> None:
            data = (STATIC_DIR / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _events(self, investigation_id: str) -> None:
            app.store.get_investigation(investigation_id)  # 404 early if unknown
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            last_digest = None
            try:
                while True:
                    snapshot = app.snapshot(investigation_id)
                    payload = json.dumps(snapshot, ensure_ascii=False)
                    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
                    if digest != last_digest:
                        self.wfile.write(f"event: state\ndata: {payload}\n\n".encode("utf-8"))
                        last_digest = digest
                    else:
                        # A comment frame each tick costs nothing and makes a closed
                        # socket fail fast instead of polling SQLite forever.
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    time.sleep(0.5)
            except Exception:  # client went away, or the store did (tests); either way stop streaming
                return

        # -- POST
        def do_POST(self) -> None:
            _, parts = self._route()
            try:
                body = self._body()
                if parts == ["api", "investigations"]:
                    budget = int(body.get("budget", 30))
                    if not 1 <= budget <= 100:
                        raise ValueError("budget must be between 1 and 100")
                    investigation_id = app.start_investigation(str(body.get("repository", "")), budget)
                    return self._json(202, {"id": investigation_id})
                if parts[:2] == ["api", "investigations"] and len(parts) == 4:
                    investigation_id = self._resolve(parts[2])
                    action = parts[3]
                    if action == "chat":
                        app.chat(investigation_id, str(body.get("message", "")))
                    elif action == "approve":
                        app.approve(investigation_id, int(body.get("calls", 0)))
                    elif action == "finalize":
                        app.finalize(investigation_id)
                    elif action == "resume":
                        app.resume(investigation_id)
                    else:
                        return self._json(404, {"error": "Unknown action"})
                    return self._json(202, {"ok": True})
                self._json(404, {"error": "Not found"})
            except KeyError as exc:
                self._json(404, {"error": str(exc)})
            except (ValueError, BudgetExhausted, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})

    return Handler


def serve(app: WebApp, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(app))
    server.daemon_threads = True
    return server
