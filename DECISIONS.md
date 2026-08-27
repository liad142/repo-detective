# Decisions

## Intake vs. agent vs. chat

**Intake is deterministic code.** It validates a `github.com` URL or `owner/repo`, follows GitHub redirects, and fetches only the required starting facts: repository metadata, latest GitHub Release, and top contributors. Archive status, dates, license, and fork counts come from the same responses. Intake makes no adoption judgment and never calls an LLM.

**The agent owns investigative judgment.** It receives the intake snapshot, the adoption goal, a compact evidence ledger, prior actions, and bounded read-only GitHub tools. There is no fixed execution checklist. Each tool call must state the question it resolves and cite the evidence IDs that triggered it. Raw GitHub responses are stored, while only normalized bounded facts enter the model context. A verdict is accepted only if every claim cites evidence from the same investigation.

**Chat is intentionally narrower.** A question is answered from the stored verdict/log/evidence and has no GitHub tools. A request for new work becomes a re-task and resumes the original investigation with its remaining budget. Chat LLM calls are tracked separately because asking for an explanation should not consume the research budget; only calls that can collect evidence or change the verdict count toward the initial 30.

## Budget and state

Every outbound investigation LLM request is reserved atomically in SQLite before dispatch, so restarts cannot silently exceed the limit. When one call remains, research tools are removed and only `submit_verdict` or `request_more_budget` is available. Budget requests contain a provisional verdict, unresolved questions, proposed checks, requested calls, and expected decision impact. Approval is an explicit persisted event.

SQLite was chosen over in-memory state or a hosted database because separate Docker commands must share the log, chat, remaining budget, and verdict without additional infrastructure. Markdown reports are rendered with plain Python from stored structures; no report-writing LLM call is hidden.

A provider failure (network, 5xx, timeout) pauses the investigation as `paused_external` with the attempt counted; `resume` continues it with the remaining budget rather than starting a fresh 30-call investigation. A re-task that arrives after the budget is spent is persisted as a new revision before the budget pause, so the approved run actually executes the human's instruction. GitHub responses are cached only when they are stable facts (2xx, 404); a rate-limit 403 is never cached, otherwise one throttled call would poison the next fifteen minutes.

## Provider and framework choice

The LLM adapter uses the OpenAI-compatible `/chat/completions` function-calling contract via Python's standard HTTP library. The key, base URL, and model are configuration. No vendor SDK, LangChain, or LangGraph is used. This keeps the execution path explainable and the Docker image dependency-free. The selected provider must support Chat Completions function tools.

Every turn is a fresh two-message conversation (system prompt plus a JSON context of intake, ledger, log, budget). Nothing depends on `tool`-role message history, which is the part of the contract gateways implement least consistently. `tool_choice: "required"` is sent so the model must act; the one exception to "no automatic retry" is an HTTP 400 that names `tool_choice`, which is a schema rejection rather than an inference call, so the client downgrades to `auto` once and continues. If a provider returns several parallel tool calls, the first is executed and the rest are logged as discarded rather than burning a budgeted call on a retry.

## Cut for the timebox

- No browser UI; the assignment explicitly allows CLI.
- No OSV integration; GitHub repository and global advisory APIs are implemented first.
- No full repository clone or static code analysis.
- No vector database; a 30-step bounded investigation fits a compact evidence ledger.
- Advisory cursor pagination is limited to the first bounded page and disclosed in tool limitations.
- No automatic provider retry because every outbound attempt must be visible in the call budget (the sole exception is the `tool_choice` schema probe above).
- No recorded evals with a real model; `tests/fake_openai_server.py` is a scripted provider that drives the full loop offline against live GitHub.

## With two more weeks

I would add recorded evals for diverse repositories, conditional requests and cursor continuation for all endpoints, package-manifest detection across ecosystems, OSV correlation, richer maintainer-response metrics, prompt-injection hardening for repository text, and a small read-only web timeline. I would also test several OpenAI-compatible gateways and add a capability probe so unsupported tool parameters fail before an investigation begins.

