# Decisions

## Intake vs. agent vs. chat

**Intake is deterministic code.** It validates a `github.com` URL or `owner/repo`, follows GitHub's redirect for renamed repositories, and fetches only the starting facts: repository metadata, latest release, top contributors. It makes no adoption judgment and never calls an LLM, so the starting point costs zero budget.

**The agent owns every investigative judgment.** It receives the intake snapshot, the goal, a compact evidence ledger, its own prior steps, and bounded read-only GitHub tools. There is no checklist. Every tool call must state the question it resolves and cite the evidence IDs that triggered it. Raw API responses are stored; only normalized, bounded facts enter the prompt. A verdict is accepted only if every claim cites evidence from the same investigation — enforced by code, not by the prompt.

**Chat is deliberately narrower.** A question is answered from the stored verdict, log, and evidence, with no GitHub tools, and does not consume research budget. A request for new work becomes a re-task: same investigation, revision + 1, same remaining budget, same agent. There is exactly one code path that can change a verdict.

## Budget and state

Every outbound investigation LLM request is reserved atomically in SQLite before it is sent, so restarts and failures cannot exceed the limit; failed attempts count. When one call remains, only `submit_verdict` and `request_more_budget` are offered. A budget request carries a provisional verdict, unresolved questions, proposed checks, and the expected impact; approval and denial are persisted events. A provider failure pauses the run as `paused_external`; `resume` continues it. A re-task that arrives after the budget is spent is stored first, then the pause is requested, so the approved run executes the human's instruction.

SQLite was chosen because separate Docker commands must share budget, log, chat, and verdict with no other infrastructure. Reports are rendered from stored structures by plain Python. GitHub responses are cached only when they are stable facts (2xx, 404); a rate-limit 403 is never cached.

## Provider and framework choice

The LLM adapter speaks the plain Chat Completions function-calling contract over `urllib`; key, base URL, and model are configuration. No SDK, LangChain, or LangGraph: the loop is 400 lines of visible Python and nothing hides a call from the budget. Each turn is a fresh two-message prompt carrying the full state, which avoids `tool`-role message history that gateways implement inconsistently and lets a run resume from the database alone. `tool_choice: "required"` is sent; the single automatic retry is a 400 that names `tool_choice`, a schema rejection rather than inference, after which the client downgrades to `auto`. Parallel tool calls execute the first and log the rest.

The web UI is a second adapter over the same services, like the CLI: stdlib `http.server`, Server-Sent Events derived from SQLite, no auth, bound to localhost.

## Cut for the timebox

- No hosted web service; hosting needs auth and a spend cap.
- No repository clone or static analysis; files are read through the API, bounded.
- No vector store; a 30-step ledger fits in context.
- Advisory pagination stops at the first page and says so in the tool's limitations.
- No recorded evals with a real model; `tests/fake_openai_server.py` drives the full loop offline against live GitHub.

## With two more weeks

Recorded evals over a diverse repository set; manifest detection at intake across ecosystems so package identity is known before any advisory query; cursor continuation for advisories; maintainer-response latency metrics; prompt-injection hardening of repository text; a provider capability probe before the first budgeted call.
