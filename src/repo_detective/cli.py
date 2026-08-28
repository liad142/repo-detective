from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any

from .agent import InvestigationAgent
from .chat import GroundedChatService
from .config import ConfigurationError, Settings
from .github import GitHubClient, IntakeService, parse_repository_input
from .llm import OpenAICompatibleClient
from .models import BudgetExhausted, InvestigationStatus
from .report import ReportRenderer
from .storage import InvestigationStore
from .tools import GitHubToolRegistry


DEFAULT_GOAL = "Should our engineering team adopt this open-source project?"


@dataclass(slots=True)
class Runtime:
    settings: Settings
    store: InvestigationStore
    reports: ReportRenderer
    intake: IntakeService | None = None
    agent: InvestigationAgent | None = None
    chat: GroundedChatService | None = None


def build_runtime(*, require_llm: bool) -> Runtime:
    settings = Settings.from_env(require_llm=require_llm)
    store = InvestigationStore(settings.database_path)
    reports = ReportRenderer(store, settings)
    if not require_llm:
        return Runtime(settings=settings, store=store, reports=reports)

    github = GitHubClient(settings, store)
    intake = IntakeService(github, store)
    llm = OpenAICompatibleClient(settings)
    tools = GitHubToolRegistry(github, store, settings)
    agent = InvestigationAgent(store, llm, tools, reports)
    chat = GroundedChatService(store, llm, agent, reports)
    return Runtime(
        settings=settings,
        store=store,
        reports=reports,
        intake=intake,
        agent=agent,
        chat=chat,
    )


def resolve_id(store: InvestigationStore, value: str) -> str:
    return store.latest_investigation_id() if value == "latest" else value


def cmd_investigate(args: argparse.Namespace) -> int:
    runtime = build_runtime(require_llm=True)
    owner, repo = parse_repository_input(args.repository)
    investigation_id = runtime.store.create_investigation(
        input_url=args.repository,
        owner=owner,
        repo=repo,
        goal=args.goal,
        initial_budget=args.budget,
    )
    print(f"Investigation: {investigation_id}")
    print(f"Repository: {owner}/{repo}")
    print("Running deterministic intake...")
    assert runtime.intake is not None and runtime.agent is not None
    try:
        intake = runtime.intake.run(investigation_id)
    except Exception as exc:
        report = runtime.reports.render(investigation_id)
        print(f"Intake failed safely: {exc}", file=sys.stderr)
        print(f"Report: {report}")
        return 2

    print(
        f"Intake complete: {intake['canonical_full_name']} "
        f"({intake['stars']} stars, archived={intake['archived']})"
    )
    print("Starting adaptive investigation...")
    investigation = runtime.agent.run(investigation_id)
    report = runtime.reports.render(investigation_id)
    print_result(investigation, report)
    return 0 if investigation["status"] in {
        InvestigationStatus.COMPLETED.value,
        InvestigationStatus.AWAITING_BUDGET.value,
    } else 2


def cmd_chat(args: argparse.Namespace) -> int:
    runtime = build_runtime(require_llm=True)
    assert runtime.chat is not None and runtime.agent is not None
    investigation_id = resolve_id(runtime.store, args.investigation_id)
    investigation = runtime.store.get_investigation(investigation_id)
    print(
        f"Chatting about {investigation.get('canonical_full_name') or investigation['input_url']} "
        f"[{investigation_id}]"
    )
    print("Questions use only stored evidence. New research resumes the same investigation.")
    print("Commands: /approve N, /finalize, /status, /report, /exit")

    if args.ask:
        outcome = runtime.chat.ask(investigation_id, args.ask)
        print_chat_outcome(outcome)
        return 0

    while True:
        try:
            message = input("repo-detective> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not message:
            continue
        if message in {"/exit", "/quit"}:
            return 0
        if message == "/status":
            print_status(runtime.store.get_investigation(investigation_id))
            continue
        if message == "/report":
            print(runtime.reports.render(investigation_id))
            continue
        if message == "/finalize":
            runtime.store.finalize_provisional(investigation_id)
            print(f"Finalized: {runtime.reports.render(investigation_id)}")
            continue
        if message.startswith("/approve "):
            try:
                amount = int(message.split(maxsplit=1)[1])
                runtime.store.approve_budget(
                    investigation_id, amount, "Approved by human in grounded chat"
                )
                print(f"Approved {amount} calls. Resuming investigation...")
                runtime.agent.run(investigation_id)
                print_status(runtime.store.get_investigation(investigation_id))
            except ValueError as exc:
                print(f"Approval failed: {exc}")
            continue

        outcome = runtime.chat.ask(investigation_id, message)
        print_chat_outcome(outcome)


def cmd_approve(args: argparse.Namespace) -> int:
    runtime = build_runtime(require_llm=True)
    assert runtime.agent is not None
    investigation_id = resolve_id(runtime.store, args.investigation_id)
    runtime.store.approve_budget(
        investigation_id,
        args.calls,
        args.reason or "Approved by human via CLI",
    )
    print(f"Approved {args.calls} additional calls. Resuming...")
    investigation = runtime.agent.run(investigation_id)
    report = runtime.reports.render(investigation_id)
    print_result(investigation, report)
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    """Continue an investigation stranded by a provider failure, with its remaining budget."""
    runtime = build_runtime(require_llm=True)
    assert runtime.agent is not None
    investigation_id = resolve_id(runtime.store, args.investigation_id)
    runtime.store.resume_after_external_pause(investigation_id)
    print("Resuming investigation with its remaining budget...")
    investigation = runtime.agent.run(investigation_id)
    report = runtime.reports.render(investigation_id)
    print_result(investigation, report)
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    """Local web UI over the same services. Not an authenticated service; bind to localhost."""
    from .web import WebApp, serve

    runtime = build_runtime(require_llm=True)
    server = serve(WebApp(runtime), args.host, args.port)
    print(f"Repo Detective web UI on http://{args.host}:{args.port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
    return 0


def cmd_finalize(args: argparse.Namespace) -> int:
    runtime = build_runtime(require_llm=False)
    investigation_id = resolve_id(runtime.store, args.investigation_id)
    runtime.store.finalize_provisional(investigation_id)
    report = runtime.reports.render(investigation_id)
    print(f"Finalized using the provisional verdict. Report: {report}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    runtime = build_runtime(require_llm=False)
    investigation_id = resolve_id(runtime.store, args.investigation_id)
    path = runtime.reports.render(investigation_id)
    if args.stdout:
        print(path.read_text(encoding="utf-8"))
    else:
        print(path)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    del args
    runtime = build_runtime(require_llm=False)
    rows = runtime.store.list_investigations()
    if not rows:
        print("No investigations found.")
        return 0
    for item in rows:
        total = item["initial_budget"] + item["approved_extra_budget"]
        name = item["canonical_full_name"] or f"{item['owner']}/{item['repo']}"
        print(
            f"{item['id']}  {name:<35}  {item['status']:<18} "
            f"calls={item['investigation_calls_used']}/{total}  r{item['revision']}"
        )
    return 0


def print_result(investigation: dict[str, Any], report: Any) -> None:
    print_status(investigation)
    print(f"Report: {report}")


def print_status(investigation: dict[str, Any]) -> None:
    print(f"Status: {investigation['status']}")
    print(
        f"Investigation LLM calls: {investigation['investigation_calls_used']} / "
        f"{investigation['initial_budget'] + investigation['approved_extra_budget']} "
        f"(remaining {investigation['remaining_budget']})"
    )
    verdict = investigation.get("verdict")
    if verdict:
        print(
            f"Verdict: {verdict.get('decision')} "
            f"(confidence {verdict.get('confidence')})"
        )
    pending = investigation.get("pending_budget_request")
    if pending:
        print(f"Human approval requested: {pending.get('requested_calls')} calls")
        print(pending.get("expected_verdict_impact", ""))
    if investigation.get("last_error"):
        print(f"Last error: {investigation['last_error']}")


def print_chat_outcome(outcome: dict[str, Any]) -> None:
    print(outcome.get("answer", json.dumps(outcome, indent=2)))
    if outcome.get("evidence_ids"):
        print("Evidence: " + ", ".join(outcome["evidence_ids"]))
    if outcome.get("verdict"):
        print(f"Updated verdict: {outcome['verdict'].get('decision')}")
    if outcome.get("report"):
        print(f"Report: {outcome['report']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repo-detective",
        description="Evidence-grounded AI investigations of public GitHub repositories.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    investigate = subparsers.add_parser("investigate", help="Investigate a public repository")
    investigate.add_argument("repository", help="github.com URL or owner/repo")
    investigate.add_argument("--goal", default=DEFAULT_GOAL)
    investigate.add_argument("--budget", type=int, default=30)
    investigate.set_defaults(func=cmd_investigate)

    chat = subparsers.add_parser("chat", help="Question or re-task a saved investigation")
    chat.add_argument("investigation_id", nargs="?", default="latest")
    chat.add_argument("--ask", help="Ask one non-interactive chat question")
    chat.set_defaults(func=cmd_chat)

    approve = subparsers.add_parser("approve", help="Approve additional LLM calls and resume")
    approve.add_argument("investigation_id", nargs="?", default="latest")
    approve.add_argument("--calls", type=int, required=True)
    approve.add_argument("--reason")
    approve.set_defaults(func=cmd_approve)

    resume = subparsers.add_parser(
        "resume", help="Continue an investigation paused by an LLM provider failure"
    )
    resume.add_argument("investigation_id", nargs="?", default="latest")
    resume.set_defaults(func=cmd_resume)

    web = subparsers.add_parser("web", help="Serve the local web UI")
    web.add_argument("--host", default="0.0.0.0")
    web.add_argument("--port", type=int, default=8080)
    web.set_defaults(func=cmd_web)

    finalize = subparsers.add_parser(
        "finalize", help="Finalize the provisional verdict without extra calls"
    )
    finalize.add_argument("investigation_id", nargs="?", default="latest")
    finalize.set_defaults(func=cmd_finalize)

    report = subparsers.add_parser("report", help="Re-render a report from stored data")
    report.add_argument("investigation_id", nargs="?", default="latest")
    report.add_argument("--stdout", action="store_true", help="Print the Markdown report")
    report.set_defaults(func=cmd_report)

    listing = subparsers.add_parser("list", help="List saved investigations")
    listing.set_defaults(func=cmd_list)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "budget", 30) <= 0 or getattr(args, "budget", 30) > 100:
        parser.error("--budget must be between 1 and 100")
    try:
        return int(args.func(args))
    except (ConfigurationError, KeyError, ValueError, BudgetExhausted) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
