# Decisions

## Intake vs. agent vs. chat

**Intake is deterministic code.** It validates a `github.com` URL or `owner/repo`, follows GitHub's redirect for renamed repositories, and fetches only the starting facts: metadata, latest release, top contributors. No judgment, no LLM, zero budget.

**The agent owns every investigative judgment.** It gets the intake snapshot, the goal, a compact evidence ledger, its own prior steps, and bounded read-only GitHub tools. No checklist. Every tool call must state the question it resolves and cite the evidence IDs that triggered it. Raw responses are stored; only normalized, bounded facts enter the prompt. A verdict is accepted only if every claim cites evidence from the same investigation — enforced by code. One prompt rule keeps dependency reasoning honest: a declared range is not a resolved version, exposure is claimed only when a resolved version was observed, and routine lockfile or scanning hygiene is not a project-specific condition.

**Chat is deliberately narrower.** A question is answered from the stored verdict, log, and evidence, with no GitHub tools and no research budget. New work becomes a re-task: same investigation, revision + 1, same remaining budget, same agent. One code path can change a verdict.

## Budget and state

Every outbound LLM request is reserved atomically in SQLite before it is sent; failed attempts count. With one call left, only `submit_verdict` and `request_more_budget` are offered. A budget request carries a provisional verdict, unresolved questions, proposed checks, and expected impact; approval and denial are persisted events. A provider failure pauses the run; `resume` continues it. If a process was killed mid-call, `resume` first closes out the reserved call and any unfinished step in one transaction, without refunding budget.

SQLite because separate Docker commands must share budget, log, chat, and verdict with no other infrastructure. Reports are plain Python over stored rows. GitHub responses are cached only when they are stable facts (2xx, 404); a rate-limit 403 never is.

## Provider and framework choice

Plain Chat Completions function calling over `urllib`; key, base URL, and model are configuration. No SDK or agent framework: the loop is a few hundred lines of visible Python and nothing hides a call from the budget. Each turn is a fresh two-message prompt carrying the full state, which avoids `tool`-role history that gateways implement inconsistently and lets a run resume from the database alone. `tool_choice: "required"` is sent; the one automatic retry is a 400 naming `tool_choice`, after which the client downgrades to `auto`.

The web UI is a second adapter over the same services: stdlib `http.server`, Server-Sent Events derived from SQLite, no auth, localhost only. Hosting it was cut on purpose.

## Cut for the timebox

Hosted service (needs auth and a spend cap); repository clone or static analysis; vector store; advisory pagination beyond the first page; recorded evals with a real model — `tests/fake_openai_server.py` drives the loop offline against live GitHub instead.

## With two more weeks

Recorded evals over a diverse repository set; lockfile awareness so resolved versions are observed rather than inferred; advisory pagination; maintainer-response latency; prompt-injection hardening of repository text.
