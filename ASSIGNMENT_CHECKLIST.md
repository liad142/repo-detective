# Assignment Coverage Checklist

| Requirement | Implementation | Verification |
|---|---|---|
| Input any public GitHub repository | Strict `github.com` / `owner/repo` parser and canonical redirect handling | `test_github.py` |
| Intake is plain code, no LLM | `IntakeService` calls GitHub directly | `IntakeTests` |
| Name, description, stars, last release, top contributors | Stored in `intake_json` with three evidence records | `IntakeTests` |
| Agent chooses what to check | One model-selected tool per turn; no fixed sequence | Agent loop and prompt |
| Commits | `list_commits`, `get_commit`, `compare_refs` | Bounded GitHub tools |
| Contributors | `list_contributors` with sample concentration | Bounded GitHub tools |
| Issues | `list_issues`, `inspect_issue` with comments | Bounded GitHub tools |
| Pull requests | `list_pull_requests` | Bounded GitHub tools |
| Releases and tags | `list_releases`, `list_tags` | Bounded GitHub tools |
| Security advisories | Repository advisories and global advisory DB, including malware | Bounded GitHub tools |
| Follow fork/community leads | `list_forks`; every repo-aware tool accepts a fork target | Tool schemas |
| 30 LLM call hard budget | Atomic reservation; control-only final call; parallel tool calls never cost a retry | `test_agent.py`, `test_storage.py` |
| Human approval for more | `request_more_budget`, `approve`, `/approve N`, budget events | Storage and CLI; fake-provider E2E |
| Graceful pause, no crash or overrun | Provider failure → `paused_external`, `resume` continues with remaining budget | `test_storage.py`; fake-provider E2E |
| Show current evidence and value of more calls | Structured budget request and interim report | Report renderer |
| Investigation log: what, why, result | Steps store rationale, question, trigger evidence, action, observation | SQLite and report |
| Three verdicts | Validated enum: adopt / conditions / reject | `test_models.py` |
| Evidence over model memory | Evidence-only prompt plus same-investigation ID validation | Verdict validation tests |
| Readable report from plain code | `ReportRenderer`, no LLM dependency | Markdown output |
| Grounded chat | Only answer/resume tools; no GitHub tools for Q&A; last 10 messages as context | `test_chat.py`, `test_chat_resume.py` |
| Re-task resumes remaining budget | Revision increment and same SQLite investigation; exhausted budget stores the re-task then pauses for approval | `test_chat_resume.py` |
| Re-task may update verdict | Agent runs again and saves a new report revision | Fake-provider E2E (verdict changed r1→r2) |
| Renamed repositories | HTTP redirects followed; canonical `full_name` stored | GitHub client/intake |
| 404, empty, archived, missing releases | Explicit verification states and safe intake behavior | GitHub client/intake; fake-provider E2E (404 exits 2 with report) |
| Do not invent unverifiable facts | unavailable/not-found/rate-limited are distinct evidence states; rate limits are never cached | `test_github.py` |
| GitHub works without key | `GITHUB_TOKEN` optional | Configuration |
| OSV optional bonus | `query_osv` tool: keyless POST to `api.osv.dev/v1/query`, ecosystem mapping, bounded normalization, same evidence path | `test_osv.py` |
| LLM key from environment | `OPENAI_API_KEY`; never persisted | Config/security design |
| OpenAI-compatible base URL and model | `OPENAI_BASE_URL`, `OPENAI_MODEL`; direct Chat Completions HTTP; `tool_choice` probe falls back to `auto`; separate LLM timeout | `test_llm.py` |
| No hardcoded model/vendor logic | Model required from environment | Config tests/manual review |
| Docker, one investigate command, one chat command | Dockerfile and Compose commands in README | Smoke test |
| Chat: CLI or web | CLI chat plus a local web UI (`docker compose up web`) with live log, chat, approve/finalize/resume | `test_web.py`; browser run |
| Public clone/run in five minutes | Dependency-free image and documented setup | Docker smoke test |
| One-page `DECISIONS.md` | Included | Manual review |
| No secrets committed | `.env` ignored; `.env.example` placeholders | Repository scan |

