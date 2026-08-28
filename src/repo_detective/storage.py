from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .models import (
    BudgetExhausted,
    InvestigationStatus,
    json_dumps,
    json_loads,
    utc_now,
)


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS investigations (
    id TEXT PRIMARY KEY,
    input_url TEXT NOT NULL,
    owner TEXT NOT NULL,
    repo TEXT NOT NULL,
    canonical_full_name TEXT,
    html_url TEXT,
    goal TEXT NOT NULL,
    status TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    initial_budget INTEGER NOT NULL DEFAULT 30,
    approved_extra_budget INTEGER NOT NULL DEFAULT 0,
    investigation_calls_used INTEGER NOT NULL DEFAULT 0,
    chat_calls_used INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    intake_json TEXT,
    verdict_json TEXT,
    pending_budget_request_json TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS investigation_steps (
    id TEXT PRIMARY KEY,
    investigation_id TEXT NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    llm_call_number INTEGER,
    action_type TEXT NOT NULL,
    rationale TEXT NOT NULL,
    question_to_answer TEXT,
    based_on_evidence_ids_json TEXT NOT NULL,
    tool_name TEXT,
    tool_arguments_json TEXT,
    result_status TEXT,
    observation TEXT,
    evidence_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(investigation_id, sequence)
);

CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    investigation_id TEXT NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
    step_id TEXT REFERENCES investigation_steps(id) ON DELETE SET NULL,
    source TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    api_url TEXT NOT NULL,
    html_url TEXT,
    request_parameters_json TEXT NOT NULL,
    http_status INTEGER NOT NULL,
    verification_status TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    summary TEXT NOT NULL,
    normalized_json TEXT NOT NULL,
    rate_limit_remaining INTEGER,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id TEXT PRIMARY KEY,
    investigation_id TEXT NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
    purpose TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    status TEXT NOT NULL,
    model TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    completed_at TEXT,
    provider_request_id TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    error TEXT
);

CREATE TABLE IF NOT EXISTS budget_events (
    id TEXT PRIMARY KEY,
    investigation_id TEXT NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    amount INTEGER NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id TEXT PRIMARY KEY,
    investigation_id TEXT NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    mode TEXT NOT NULL,
    content TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS http_cache (
    cache_key TEXT PRIMARY KEY,
    status INTEGER NOT NULL,
    final_url TEXT NOT NULL,
    headers_json TEXT NOT NULL,
    body_json TEXT,
    etag TEXT,
    fetched_at_epoch REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_steps_investigation
ON investigation_steps(investigation_id, sequence);

CREATE INDEX IF NOT EXISTS idx_evidence_investigation
ON evidence(investigation_id, retrieved_at);

CREATE INDEX IF NOT EXISTS idx_chat_investigation
ON chat_messages(investigation_id, created_at);
"""


class InvestigationStore:
    """SQLite-backed source of truth for state, budget, logs, evidence, and chat."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript(SCHEMA)
            conn.execute("PRAGMA journal_mode = WAL")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_investigation(
        self,
        *,
        input_url: str,
        owner: str,
        repo: str,
        goal: str,
        initial_budget: int = 30,
    ) -> str:
        investigation_id = str(uuid.uuid4())
        now = utc_now()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO investigations (
                    id, input_url, owner, repo, goal, status, initial_budget,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    investigation_id,
                    input_url,
                    owner,
                    repo,
                    goal,
                    InvestigationStatus.CREATED.value,
                    initial_budget,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO budget_events
                (id, investigation_id, event_type, amount, reason, created_at)
                VALUES (?, ?, 'initialized', ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    investigation_id,
                    initial_budget,
                    "Initial investigation budget",
                    now,
                ),
            )
        return investigation_id

    def get_investigation(self, investigation_id: str) -> dict[str, Any]:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM investigations WHERE id = ?", (investigation_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Investigation not found: {investigation_id}")
        result = dict(row)
        result["intake"] = json_loads(result.pop("intake_json"), None)
        result["verdict"] = json_loads(result.pop("verdict_json"), None)
        result["pending_budget_request"] = json_loads(
            result.pop("pending_budget_request_json"), None
        )
        result["remaining_budget"] = (
            result["initial_budget"]
            + result["approved_extra_budget"]
            - result["investigation_calls_used"]
        )
        return result

    def latest_investigation_id(self) -> str:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT id FROM investigations ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            raise KeyError("No investigations exist")
        return str(row["id"])

    def list_investigations(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, canonical_full_name, owner, repo, status, revision,
                       investigation_calls_used, initial_budget,
                       approved_extra_budget, created_at, updated_at
                FROM investigations ORDER BY created_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def set_status(
        self, investigation_id: str, status: InvestigationStatus, error: str | None = None
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE investigations
                SET status = ?, last_error = ?, updated_at = ? WHERE id = ?
                """,
                (status.value, error, utc_now(), investigation_id),
            )

    def save_intake(
        self,
        investigation_id: str,
        intake: dict[str, Any],
        *,
        canonical_full_name: str,
        html_url: str,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE investigations
                SET intake_json = ?, canonical_full_name = ?, html_url = ?,
                    status = ?, updated_at = ?, last_error = NULL
                WHERE id = ?
                """,
                (
                    json_dumps(intake),
                    canonical_full_name,
                    html_url,
                    InvestigationStatus.INVESTIGATING.value,
                    utc_now(),
                    investigation_id,
                ),
            )

    def reserve_llm_call(self, investigation_id: str, purpose: str, model: str) -> dict[str, Any]:
        if purpose not in {"investigation", "chat"}:
            raise ValueError("purpose must be investigation or chat")
        call_id = str(uuid.uuid4())
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT initial_budget, approved_extra_budget,
                       investigation_calls_used, chat_calls_used
                FROM investigations WHERE id = ?
                """,
                (investigation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Investigation not found: {investigation_id}")

            if purpose == "investigation":
                total = row["initial_budget"] + row["approved_extra_budget"]
                if row["investigation_calls_used"] >= total:
                    raise BudgetExhausted("The investigation LLM budget is exhausted")
                ordinal = row["investigation_calls_used"] + 1
                conn.execute(
                    """
                    UPDATE investigations
                    SET investigation_calls_used = investigation_calls_used + 1,
                        updated_at = ? WHERE id = ?
                    """,
                    (utc_now(), investigation_id),
                )
            else:
                ordinal = row["chat_calls_used"] + 1
                conn.execute(
                    """
                    UPDATE investigations
                    SET chat_calls_used = chat_calls_used + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (utc_now(), investigation_id),
                )

            requested_at = utc_now()
            conn.execute(
                """
                INSERT INTO llm_calls
                (id, investigation_id, purpose, ordinal, status, model, requested_at)
                VALUES (?, ?, ?, ?, 'reserved', ?, ?)
                """,
                (call_id, investigation_id, purpose, ordinal, model, requested_at),
            )
        return {"id": call_id, "ordinal": ordinal, "purpose": purpose}

    def complete_llm_call(
        self,
        call_id: str,
        *,
        provider_request_id: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE llm_calls SET status = 'completed', completed_at = ?,
                    provider_request_id = ?, input_tokens = ?, output_tokens = ?
                WHERE id = ?
                """,
                (utc_now(), provider_request_id, input_tokens, output_tokens, call_id),
            )

    def fail_llm_call(self, call_id: str, error: str) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE llm_calls SET status = 'failed', completed_at = ?, error = ?
                WHERE id = ?
                """,
                (utc_now(), error[:4_000], call_id),
            )

    def list_llm_calls(self, investigation_id: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT purpose, ordinal, status, requested_at, completed_at, error
                FROM llm_calls WHERE investigation_id = ? ORDER BY requested_at
                """,
                (investigation_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_step(
        self,
        investigation_id: str,
        *,
        revision: int,
        llm_call_number: int | None,
        action_type: str,
        rationale: str,
        question_to_answer: str | None,
        based_on_evidence_ids: list[str],
        tool_name: str | None,
        tool_arguments: dict[str, Any] | None,
    ) -> str:
        step_id = str(uuid.uuid4())
        with self.connection() as conn:
            sequence = conn.execute(
                """
                SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence
                FROM investigation_steps WHERE investigation_id = ?
                """,
                (investigation_id,),
            ).fetchone()["next_sequence"]
            conn.execute(
                """
                INSERT INTO investigation_steps (
                    id, investigation_id, sequence, revision, llm_call_number,
                    action_type, rationale, question_to_answer,
                    based_on_evidence_ids_json, tool_name, tool_arguments_json,
                    evidence_ids_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?)
                """,
                (
                    step_id,
                    investigation_id,
                    sequence,
                    revision,
                    llm_call_number,
                    action_type,
                    rationale,
                    question_to_answer,
                    json_dumps(based_on_evidence_ids),
                    tool_name,
                    json_dumps(tool_arguments) if tool_arguments is not None else None,
                    utc_now(),
                ),
            )
        return step_id

    def complete_step(
        self,
        step_id: str,
        *,
        result_status: str,
        observation: str,
        evidence_ids: list[str],
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE investigation_steps SET result_status = ?, observation = ?,
                    evidence_ids_json = ?, completed_at = ? WHERE id = ?
                """,
                (
                    result_status,
                    observation[:12_000],
                    json_dumps(evidence_ids),
                    utc_now(),
                    step_id,
                ),
            )

    def add_evidence(
        self,
        investigation_id: str,
        *,
        step_id: str | None,
        source: str,
        tool_name: str,
        api_url: str,
        html_url: str | None,
        request_parameters: dict[str, Any],
        http_status: int,
        verification_status: str,
        summary: str,
        normalized: dict[str, Any] | list[Any],
        rate_limit_remaining: int | None,
        raw: Any,
    ) -> str:
        evidence_id = f"EV-{uuid.uuid4().hex[:10].upper()}"
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO evidence (
                    id, investigation_id, step_id, source, tool_name, api_url,
                    html_url, request_parameters_json, http_status,
                    verification_status, retrieved_at, summary, normalized_json,
                    rate_limit_remaining, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id,
                    investigation_id,
                    step_id,
                    source,
                    tool_name,
                    api_url,
                    html_url,
                    json_dumps(request_parameters),
                    http_status,
                    verification_status,
                    utc_now(),
                    summary[:12_000],
                    json_dumps(normalized),
                    rate_limit_remaining,
                    json_dumps(raw) if raw is not None else None,
                ),
            )
        return evidence_id

    def list_steps(self, investigation_id: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM investigation_steps WHERE investigation_id = ?
                ORDER BY sequence
                """,
                (investigation_id,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["based_on_evidence_ids"] = json_loads(
                item.pop("based_on_evidence_ids_json"), []
            )
            item["tool_arguments"] = json_loads(item.pop("tool_arguments_json"), None)
            item["evidence_ids"] = json_loads(item.pop("evidence_ids_json"), [])
            result.append(item)
        return result

    def list_evidence(self, investigation_id: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM evidence WHERE investigation_id = ? ORDER BY retrieved_at",
                (investigation_id,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["request_parameters"] = json_loads(
                item.pop("request_parameters_json"), {}
            )
            item["normalized"] = json_loads(item.pop("normalized_json"), {})
            item["raw"] = json_loads(item.pop("raw_json"), None)
            result.append(item)
        return result

    def evidence_ids(self, investigation_id: str) -> set[str]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT id FROM evidence WHERE investigation_id = ?", (investigation_id,)
            ).fetchall()
        return {str(row["id"]) for row in rows}

    def complete_investigation(self, investigation_id: str, verdict: dict[str, Any]) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE investigations SET status = ?, verdict_json = ?,
                    pending_budget_request_json = NULL, updated_at = ?, last_error = NULL
                WHERE id = ?
                """,
                (
                    InvestigationStatus.COMPLETED.value,
                    json_dumps(verdict),
                    utc_now(),
                    investigation_id,
                ),
            )

    def request_budget(self, investigation_id: str, request: dict[str, Any]) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE investigations SET status = ?, pending_budget_request_json = ?,
                    verdict_json = ?, updated_at = ? WHERE id = ?
                """,
                (
                    InvestigationStatus.AWAITING_BUDGET.value,
                    json_dumps(request),
                    json_dumps(request.get("provisional_verdict")),
                    utc_now(),
                    investigation_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO budget_events
                (id, investigation_id, event_type, amount, reason, created_at)
                VALUES (?, ?, 'requested', ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    investigation_id,
                    request["requested_calls"],
                    request["expected_verdict_impact"],
                    utc_now(),
                ),
            )

    def approve_budget(self, investigation_id: str, amount: int, reason: str) -> None:
        if amount <= 0 or amount > 100:
            raise ValueError("Approved budget must be between 1 and 100 calls")
        current = self.get_investigation(investigation_id)
        if current["status"] != InvestigationStatus.AWAITING_BUDGET.value:
            raise ValueError("Additional calls can only be approved while awaiting budget")
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE investigations
                SET approved_extra_budget = approved_extra_budget + ?, status = ?,
                    pending_budget_request_json = NULL, updated_at = ? WHERE id = ?
                """,
                (
                    amount,
                    InvestigationStatus.INVESTIGATING.value,
                    utc_now(),
                    investigation_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO budget_events
                (id, investigation_id, event_type, amount, reason, created_at)
                VALUES (?, ?, 'approved', ?, ?, ?)
                """,
                (str(uuid.uuid4()), investigation_id, amount, reason, utc_now()),
            )

    def finalize_provisional(self, investigation_id: str) -> None:
        investigation = self.get_investigation(investigation_id)
        request = investigation["pending_budget_request"]
        if not request or not request.get("provisional_verdict"):
            raise ValueError("No provisional verdict is available")
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE investigations SET status = ?, verdict_json = ?,
                    pending_budget_request_json = NULL, updated_at = ? WHERE id = ?
                """,
                (
                    InvestigationStatus.COMPLETED.value,
                    json_dumps(request["provisional_verdict"]),
                    utc_now(),
                    investigation_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO budget_events
                (id, investigation_id, event_type, amount, reason, created_at)
                VALUES (?, ?, 'denied', 0, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    investigation_id,
                    "Human finalized using current evidence",
                    utc_now(),
                ),
            )

    def add_chat_message(
        self,
        investigation_id: str,
        *,
        role: str,
        mode: str,
        content: str,
        evidence_ids: list[str] | None = None,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO chat_messages
                (id, investigation_id, role, mode, content, evidence_ids_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    investigation_id,
                    role,
                    mode,
                    content,
                    json_dumps(evidence_ids or []),
                    utc_now(),
                ),
            )

    def list_chat_messages(self, investigation_id: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM chat_messages WHERE investigation_id = ? ORDER BY created_at
                """,
                (investigation_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["evidence_ids"] = json_loads(item.pop("evidence_ids_json"), [])
            result.append(item)
        return result

    def begin_retask(self, investigation_id: str, instruction: str) -> None:
        investigation = self.get_investigation(investigation_id)
        if investigation["status"] == InvestigationStatus.AWAITING_BUDGET.value:
            raise ValueError("The pending budget request must be approved or finalized first")
        if investigation["remaining_budget"] <= 0:
            raise BudgetExhausted("No investigation budget remains for re-tasking")
        self.record_retask(investigation_id, instruction, status=InvestigationStatus.INVESTIGATING)

    def record_retask(
        self, investigation_id: str, instruction: str, *, status: InvestigationStatus
    ) -> None:
        """Persist a human re-task as a new revision. The agent reads it on its next turn."""
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE investigations SET revision = revision + 1, status = ?,
                    updated_at = ?, pending_budget_request_json = NULL WHERE id = ?
                """,
                (status.value, utc_now(), investigation_id),
            )
        self.add_chat_message(
            investigation_id,
            role="user",
            mode="retask",
            content=instruction,
        )

    def resume_after_external_pause(self, investigation_id: str) -> None:
        investigation = self.get_investigation(investigation_id)
        if investigation["status"] not in {
            InvestigationStatus.PAUSED_EXTERNAL.value,
            InvestigationStatus.INVESTIGATING.value,
        }:
            raise ValueError(
                "Only investigations paused by a provider failure (or interrupted mid-run) can be resumed; "
                f"status is {investigation['status']}"
            )
        if investigation["remaining_budget"] <= 0:
            raise BudgetExhausted("No investigation budget remains; approve more calls instead")
        self.set_status(investigation_id, InvestigationStatus.INVESTIGATING)

    def get_cache(self, cache_key: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM http_cache WHERE cache_key = ?", (cache_key,)
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["headers"] = json_loads(item.pop("headers_json"), {})
        item["body"] = json_loads(item.pop("body_json"), None)
        return item

    def set_cache(
        self,
        cache_key: str,
        *,
        status: int,
        final_url: str,
        headers: dict[str, Any],
        body: Any,
        etag: str | None,
        fetched_at_epoch: float,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO http_cache
                (cache_key, status, final_url, headers_json, body_json, etag, fetched_at_epoch)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    status = excluded.status,
                    final_url = excluded.final_url,
                    headers_json = excluded.headers_json,
                    body_json = excluded.body_json,
                    etag = excluded.etag,
                    fetched_at_epoch = excluded.fetched_at_epoch
                """,
                (
                    cache_key,
                    status,
                    final_url,
                    json_dumps(headers),
                    json_dumps(body) if body is not None else None,
                    etag,
                    fetched_at_epoch,
                ),
            )
