from __future__ import annotations

import json
from typing import Any

from .agent import InvestigationAgent
from .llm import OpenAICompatibleClient
from .models import (
    BudgetExhausted,
    DomainValidationError,
    InvestigationStatus,
    require_string,
    require_string_list,
)
from .prompts import GROUNDED_CHAT_SYSTEM_PROMPT
from .report import ReportRenderer
from .storage import InvestigationStore


ANSWER_TOOL = {
    "type": "function",
    "function": {
        "name": "answer_from_log",
        "description": "Answer only from the stored investigation log and evidence.",
        "parameters": {
            "type": "object",
            "properties": {
                "answer": {"type": "string"},
                "evidence_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["answer", "evidence_ids"],
            "additionalProperties": False,
        },
    },
}

RESUME_TOOL = {
    "type": "function",
    "function": {
        "name": "resume_investigation",
        "description": "Send the agent back to investigate a new or unresolved question.",
        "parameters": {
            "type": "object",
            "properties": {
                "instruction": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["instruction", "reason"],
            "additionalProperties": False,
        },
    },
}


# An answer with no evidence IDs is only accepted when it plainly says the
# investigation has nothing on the subject.
UNSUPPORTED_ANSWER_PHRASES = (
    "not verified",
    "did not verify",
    "not established",
    "not investigated",
    "did not investigate",
    "did not check",
    "not checked",
    "no evidence",
    "no stored evidence",
    "unverified",
    "was not examined",
    "did not examine",
)

CHAT_HISTORY_LIMIT = 10


class GroundedChatService:
    def __init__(
        self,
        store: InvestigationStore,
        llm: OpenAICompatibleClient,
        agent: InvestigationAgent,
        reports: ReportRenderer,
    ):
        self.store = store
        self.llm = llm
        self.agent = agent
        self.reports = reports

    def ask(self, investigation_id: str, user_message: str) -> dict[str, Any]:
        message = user_message.strip()
        if not message:
            raise ValueError("Chat message must not be empty")
        investigation = self.store.get_investigation(investigation_id)
        self.store.add_chat_message(
            investigation_id, role="user", mode="question", content=message
        )

        call = self.store.reserve_llm_call(
            investigation_id, purpose="chat", model=self.llm.model
        )
        try:
            response = self.llm.choose_tool(
                messages=self._build_messages(investigation, message),
                tools=[ANSWER_TOOL, RESUME_TOOL],
            )
            self.store.complete_llm_call(
                call["id"],
                provider_request_id=response.provider_request_id,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
            )
        except Exception as exc:
            self.store.fail_llm_call(call["id"], str(exc))
            answer = f"The grounded chat could not reach the LLM provider: {exc}"
            self.store.add_chat_message(
                investigation_id, role="assistant", mode="error", content=answer
            )
            return {"mode": "error", "answer": answer}

        if len(response.tool_calls) != 1:
            answer = "I could not produce a grounded response because the model returned an invalid action."
            self.store.add_chat_message(
                investigation_id, role="assistant", mode="error", content=answer
            )
            return {"mode": "error", "answer": answer}

        action = response.tool_calls[0]
        if action.name == "answer_from_log":
            return self._answer(investigation_id, action.arguments)
        if action.name == "resume_investigation":
            return self._resume(investigation_id, action.arguments)

        answer = f"The grounded chat returned an unsupported action: {action.name}"
        self.store.add_chat_message(
            investigation_id, role="assistant", mode="error", content=answer
        )
        return {"mode": "error", "answer": answer}

    def _answer(self, investigation_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            answer = require_string(arguments, "answer", max_length=8_000)
            evidence_ids = require_string_list(
                arguments, "evidence_ids", allow_empty=True
            )
            unknown = sorted(set(evidence_ids) - self.store.evidence_ids(investigation_id))
            if unknown:
                raise DomainValidationError(
                    f"answer referenced unknown evidence IDs: {', '.join(unknown)}"
                )
            if not evidence_ids and not any(
                phrase in answer.lower() for phrase in UNSUPPORTED_ANSWER_PHRASES
            ):
                raise DomainValidationError(
                    "a factual answer needs evidence IDs; an unsupported answer must say it was not verified"
                )
        except DomainValidationError as exc:
            answer = (
                "The existing investigation does not contain a valid evidence-grounded "
                f"answer to that question ({exc})."
            )
            evidence_ids = []

        self.store.add_chat_message(
            investigation_id,
            role="assistant",
            mode="answer",
            content=answer,
            evidence_ids=evidence_ids,
        )
        return {"mode": "answer", "answer": answer, "evidence_ids": evidence_ids}

    def _resume(self, investigation_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
        instruction = require_string(arguments, "instruction", max_length=4_000)
        reason = require_string(arguments, "reason", max_length=2_000)
        investigation = self.store.get_investigation(investigation_id)
        if investigation["status"] == "awaiting_budget":
            pending = investigation.get("pending_budget_request") or {}
            answer = (
                "The agent is already paused for human approval"
                + (
                    f" of {pending.get('requested_calls')} additional calls."
                    if pending.get("requested_calls")
                    else "."
                )
                + " Approve or finalize that request before new research can continue."
            )
            self.store.add_chat_message(
                investigation_id, role="assistant", mode="budget_required", content=answer
            )
            return {"mode": "budget_required", "answer": answer}
        if investigation["remaining_budget"] <= 0:
            # Persist the re-task now so that, once a human approves more calls,
            # the resumed agent actually sees the instruction and the report gets
            # a new revision instead of overwriting the original.
            self.store.record_retask(
                investigation_id, instruction, status=InvestigationStatus.AWAITING_BUDGET
            )
            request = {
                "rationale": "The human requested new investigation work after the original budget was exhausted.",
                "based_on_evidence_ids": sorted(self.store.evidence_ids(investigation_id)),
                "summary_so_far": "The stored verdict remains provisional for this new question.",
                "provisional_verdict": investigation.get("verdict"),
                "unresolved_questions": [instruction],
                "proposed_next_checks": [instruction],
                "requested_calls": 5,
                "expected_verdict_impact": reason,
            }
            self.store.request_budget(investigation_id, request)
            answer = (
                "The request requires new GitHub investigation, but no investigation LLM "
                "budget remains. I paused for approval of 5 additional calls."
            )
            self.store.add_chat_message(
                investigation_id, role="assistant", mode="budget_required", content=answer
            )
            return {"mode": "budget_required", "answer": answer}
        try:
            self.store.begin_retask(investigation_id, instruction)
        except BudgetExhausted:
            return {"mode": "budget_required", "answer": "Additional budget is required."}
        self.store.add_chat_message(
            investigation_id,
            role="assistant",
            mode="retask_started",
            content=f"Resuming investigation: {instruction}\nReason: {reason}",
        )
        updated = self.agent.run(investigation_id)
        report = self.reports.render(investigation_id)
        return {
            "mode": "retask",
            "answer": f"Investigation resumed and is now {updated['status']}.",
            "status": updated["status"],
            "verdict": updated.get("verdict"),
            "report": str(report),
        }

    def _build_messages(
        self, investigation: dict[str, Any], user_message: str
    ) -> list[dict[str, str]]:
        evidence = self.store.list_evidence(investigation["id"])
        steps = self.store.list_steps(investigation["id"])
        # The current question is already stored, so exclude it from the transcript.
        history = self.store.list_chat_messages(investigation["id"])[:-1]
        context = {
            "repository": investigation.get("canonical_full_name"),
            "verdict": investigation.get("verdict"),
            "remaining_investigation_budget": investigation["remaining_budget"],
            "recent_chat": [
                {"role": item["role"], "mode": item["mode"], "content": item["content"][:2_000]}
                for item in history[-CHAT_HISTORY_LIMIT:]
            ],
            "investigation_log": [
                {
                    "step": item["sequence"],
                    "action": item["tool_name"] or item["action_type"],
                    "why": item["rationale"],
                    "result": item.get("observation"),
                    "evidence_ids": item["evidence_ids"],
                }
                for item in steps
            ],
            "evidence": [
                {
                    "id": item["id"],
                    "status": item["verification_status"],
                    "summary": item["summary"],
                    "source_url": item.get("html_url") or item.get("api_url"),
                }
                for item in evidence
            ],
            "user_question": user_message,
        }
        return [
            {"role": "system", "content": GROUNDED_CHAT_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False, indent=2)},
        ]
