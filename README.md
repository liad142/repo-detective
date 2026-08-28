# Repo Detective

Repo Detective is an evidence-grounded AI agent that investigates public GitHub repositories as open-source supply-chain decisions. It chooses its own next step, follows leads, records every action and finding, and returns one of three verdicts:

- `adopt`
- `adopt_with_conditions`
- `reject`

The implementation is intentionally small and auditable: Python 3.12 standard library, SQLite, GitHub REST, and an OpenAI-compatible Chat Completions endpoint. There is no agent framework and no hidden checklist.

## Five-minute start

Requirements: Docker and Docker Compose.

```bash
cp .env.example .env
```

Set at least:

```env
OPENAI_API_KEY=your-key
OPENAI_MODEL=your-model
```

`OPENAI_BASE_URL` defaults to `https://api.openai.com/v1`. Point it at another OpenAI-compatible provider without changing code. `GITHUB_TOKEN` is optional; public repositories work without one.

`docker compose up` builds the image and prints the CLI usage; the two entry points are the commands below.

Investigate a repository (the first run builds the image):

```bash
docker compose run --build --rm app investigate https://github.com/expressjs/express
```

Start the grounded chat over the latest investigation:

```bash
docker compose run --rm app chat latest
```

The named Docker volume preserves investigations between commands.

## Web UI (local)

The same services behind a browser page, for watching an investigation as it happens:

```bash
docker compose up web        # then open http://localhost:8080
```

Enter a repository, and the page shows intake, then each agent step as it is written to the log: the rationale and question, the evidence it was based on, a "checking" state while the GitHub call runs, the result, and the new evidence records lighting up in the ledger. Verdicts, budget requests (approve / finalize), provider pauses (resume), the grounded chat, and re-tasking all work from the page. Progress is streamed with Server-Sent Events derived from SQLite, so the page shows exactly what the log stores.

The UI has no authentication and is bound to `127.0.0.1` by compose. It is a local viewer only; hosting it (which would need authentication and a spend cap) was intentionally cut. The status bar shows calls used, calls remaining, and the last GitHub rate-limit value seen; each ledger entry can be expanded to its normalized details.

If a run is interrupted (container killed, Ctrl+C), `resume` — from the CLI or the page — first reconciles what was left behind: the reserved LLM call is marked interrupted and still counts, and any step without a result gets an explicit error observation. Nothing is refunded or double-counted.

## Assignment test cases

```bash
docker compose run --rm app investigate expressjs/express
docker compose run --rm app investigate request/request
docker compose run --rm app investigate RIAEvangelist/node-ipc
```

Results with the final code, run on 2026-08-28 with `openai/gpt-5.6-sol` through OpenRouter (`OPENAI_BASE_URL=https://openrouter.ai/api/v1`), no tuning per repository. The full reports are in [`examples/`](examples/). Verdicts depend on the model and on the repositories' state at run time.

| Repository | Verdict | Calls | Decisive evidence |
|---|---|---:|---|
| expressjs/express | adopt (high) | 11 | Active repo; OSV and malware queries clean for express@5.2.1; a body-parser advisory affects the range floor, but the declared `^2.2.1` range permits the fixed 2.3.0, and the installed version was explicitly listed as not verified |
| request/request | reject (high) | 2 | README states the project has been fully deprecated since 2020-02-11; no release, last push 2024-08-14 |
| RIAEvangelist/node-ipc | reject (high) | 7 | Reviewed advisories documenting maintainer-introduced malicious code (10.1.1–10.1.2) plus a malware advisory for 9.1.6 |

Mechanical paths observed live, independent of the model's verdict: a 404 repository ends as `intake_failed` with a report and exit code 2; `facebook/jest` resolves to the canonical `jestjs/jest`; `--budget 2` pauses at the control-only last call with a provisional verdict and a structured request, and `approve` resumes the same investigation. The three runs above cost about US$0.45 in total.

Reproduce with your own key:

```bash
docker compose run --rm app investigate expressjs/express
docker compose run --rm app report latest --stdout
```

List saved investigations:

```bash
docker compose run --rm app list
```

Print the latest Markdown report:

```bash
docker compose run --rm app report latest --stdout
```

## Human-in-the-loop budget

Each investigation starts with 30 outbound LLM calls. GitHub API calls and grounded chat calls are tracked separately. Before an investigation LLM request is sent, its call is atomically reserved in SQLite.

When one call remains, the agent is only offered two actions:

- `submit_verdict`
- `request_more_budget`

It cannot spend call 30 starting research that would require call 31 to interpret.

If more research is requested:

```bash
docker compose run --rm app approve <investigation-id> --calls 8
```

Or in interactive chat:

```text
/approve 8
```

To decline extra calls and keep the provisional verdict:

```bash
docker compose run --rm app finalize <investigation-id>
```

If the LLM provider itself fails (network error, HTTP 5xx, timeout), the investigation is saved as `paused_external` with the failed attempt counted. Continue it with its remaining budget once the provider is back:

```bash
docker compose run --rm app resume <investigation-id>
```

## Provider notes

- **OpenAI / Anthropic / most gateways:** set `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL`.
- **Azure OpenAI:** use the v1 endpoint, e.g. `OPENAI_BASE_URL=https://<resource>.openai.azure.com/openai/v1` with `OPENAI_MODEL=<deployment-name>`; it accepts the standard `Authorization: Bearer` header.
- **Local models (Ollama, LM Studio, vLLM):** from inside Docker the host is reachable as `host.docker.internal`, e.g. `OPENAI_BASE_URL=http://host.docker.internal:11434/v1`.
- The request sets `tool_choice: "required"` so the model must answer with a function call. If a provider rejects that value with HTTP 400, the client downgrades to `"auto"` for the rest of the run and says so on stderr. Set `OPENAI_TOOL_CHOICE=auto` to skip the probe. With `auto`, a prose-only reply is logged as an invalid step and costs one budgeted call.
- Slow or reasoning models: raise `LLM_TIMEOUT_SECONDS` (default 120). GitHub calls use the separate `HTTP_TIMEOUT_SECONDS`.

## Chat behavior

The chat has only two model-visible actions:

- `answer_from_log`: answer using stored verdict, log, and evidence IDs. It has no GitHub tools.
- `resume_investigation`: append a user re-task, increment the investigation revision, and resume the original agent with its remaining budget.

A request such as `now check the biggest fork` resumes research and may produce a new verdict and report revision. A question such as `why did you flag the maintainer situation?` is answered only from the stored investigation. The last ten chat messages are included so follow-up questions keep their context. If a re-task arrives after the budget is spent, the instruction is stored as a new revision and the agent pauses for `/approve N`; the approved run then starts from that instruction.

## Architecture

```mermaid
flowchart TD
    CLI[CLI] --> APP[Application services]
    WEB[Local web UI] --> APP
    APP --> DOMAIN[Domain rules and state machine]
    APP --> ADAPTERS[Adapters]
    ADAPTERS --> GH[GitHub REST]
    ADAPTERS --> LLM[OpenAI-compatible LLM]
    ADAPTERS --> DB[SQLite]
    APP --> REPORT[Deterministic Markdown report]
```

The important boundary is that the agent never receives arbitrary HTTP or database access. It sees a bounded registry of read-only GitHub tools. Tool outputs are normalized for the context window while raw public API responses remain stored as evidence.

### Investigation flow

1. **Intake, no LLM:** parse and constrain the repository input; fetch repository metadata, latest release, and top contributors.
2. **Adaptive agent:** on every turn, choose one tool based on existing evidence IDs or finish/request budget.
3. **Evidence persistence:** save raw response, normalized facts, verification status, source URL, and rate-limit state.
4. **Verdict validation:** reject claims that cite unknown evidence IDs or violate verdict invariants.
5. **Plain-code rendering:** produce a Markdown report from SQLite without an LLM call.
6. **Grounded chat:** answer from the log or re-task the same investigation.

## GitHub tools

The agent can selectively use:

- repository metadata, repository files, and directory listings (so manifests are located, not guessed)
- commits, commit details, and ref comparison
- contributors
- issues and issue comments
- pull requests
- releases and tags
- forks
- repository security advisories
- GitHub's global advisory database, including an explicit malware query
- the OSV vulnerability database (`query_osv`), keyless, as an independent second source

Every research action includes:

- `rationale`
- `question_to_answer`
- `based_on_evidence_ids`

That creates a causal log: what the agent found, which evidence changed its direction, and what it checked next.

## Failure behavior

Expected external failures become evidence states instead of crashes:

- `not_found`
- `unavailable`
- `rate_limited`
- `partial`
- `verified`

For example, an unavailable advisory endpoint cannot be interpreted as “no vulnerabilities.” Renamed repositories follow GitHub redirects and the canonical name is saved. Empty repositories, missing releases/files, archived repositories, malformed model actions, and provider errors all produce explicit status/log entries.

## Local development

No third-party Python packages are required. Python 3.12 is required (`datetime.UTC`, `StrEnum`); if your local interpreter is older, run the suite in the same base image Docker uses:

```bash
export PYTHONPATH=src
python -m unittest discover -s tests -v
python -m repo_detective --help

# or, without a local 3.12:
docker run --rm -v "$PWD:/app" -w /app -e PYTHONPATH=/app/src python:3.12-slim \
  python -m unittest discover -s tests -v
```

### Offline end-to-end smoke test

`tests/fake_openai_server.py` is a scripted OpenAI-compatible provider: it reads the same JSON context the real model receives, follows a fixed tool plan, and submits an evidence-cited verdict. It exercises real GitHub calls, the budget, the report, chat, and re-tasking without spending a real API key. Scenario switches (`FAKE_PARALLEL`, `FAKE_PROSE`, `FAKE_REJECT_REQUIRED`, `FAKE_REQUEST_BUDGET`) are documented in the file.

```bash
python tests/fake_openai_server.py &   # listens on :8089
docker compose run --rm -e OPENAI_API_KEY=fake -e OPENAI_MODEL=fake \
  -e OPENAI_BASE_URL=http://host.docker.internal:8089/v1 app investigate expressjs/express
```

Local runs use `./data` unless `DATA_DIR` is set:

```bash
PYTHONPATH=src python -m repo_detective list
```

## Configuration

| Variable | Required | Purpose |
|---|---:|---|
| `OPENAI_API_KEY` | Yes | LLM credential, never stored in SQLite |
| `OPENAI_MODEL` | Yes | Provider model name; never hardcoded |
| `OPENAI_BASE_URL` | No | OpenAI-compatible base URL |
| `OPENAI_TOOL_CHOICE` | No | `required` (default) or `auto`; see Provider notes |
| `GITHUB_TOKEN` | No | Raises GitHub rate limits when provided |
| `GITHUB_API_VERSION` | No | GitHub REST version header |
| `DATA_DIR` | No | SQLite and report directory |
| `HTTP_TIMEOUT_SECONDS` | No | GitHub request timeout (default 30) |
| `LLM_TIMEOUT_SECONDS` | No | LLM request timeout (default 120) |
| `GITHUB_CACHE_TTL_SECONDS` | No | Avoid duplicate GitHub calls; only 2xx and 404 responses are cached, never rate limits |
| `OSV_API_URL` | No | OSV endpoint (default `https://api.osv.dev`), no key needed |

## Security choices

- Only `github.com/<owner>/<repo>` or `owner/repo` input is accepted.
- GitHub tools construct allowlisted read-only REST paths; the model cannot provide arbitrary URLs.
- Repository paths reject traversal segments.
- API keys are read from the environment and never committed, logged, or persisted.
- Raw evidence is public GitHub data and is scoped to one investigation.
- Pagination and file/diff sizes are bounded.
- Reports clearly distinguish missing evidence from verified absence.

See [DECISIONS.md](DECISIONS.md) for scope decisions and [ASSIGNMENT_CHECKLIST.md](ASSIGNMENT_CHECKLIST.md) for requirement coverage.
