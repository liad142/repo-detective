from __future__ import annotations

import json
import sys
from typing import Any

from .llm import OpenAICompatibleClient
from .models import (
    BudgetExhausted,
    DomainValidationError,
    InvestigationStatus,
    ToolResultStatus,
    normalize_verdict,
    require_string,
    require_string_list,
)
from .prompts import INVESTIGATOR_SYSTEM_PROMPT, current_date_utc
from .report import ReportRenderer
from .storage import InvestigationStore
from .tools import GitHubToolRegistry


CLAIM_SCHEMA = {
    "type": "object",
    "properties": {
        "statement": {"type": "string"},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "claim_type": {"type": "string", "enum": ["observed", "inference"]},
    },
    "required": ["statement", "evidence_ids", "claim_type"],
    "additionalProperties": False,
}

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {
            "type": "string",
            "enum": ["adopt", "adopt_with_conditions", "reject"],
        },
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "executive_summary": {"type": "string"},
        "positive_signals": {"type": "array", "items": CLAIM_SCHEMA},
        "risk_factors": {"type": "array", "items": CLAIM_SCHEMA},
        "adoption_conditions": {"type": "array", "items": {"type": "string"}},
        "unverified_items": {"type": "array", "items": {"type": "string"}},
        "decisive_evidence_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "decision",
        "confidence",
        "executive_summary",
        "positive_signals",
        "risk_factors",
        "adoption_conditions",
        "unverified_items",
        "decisive_evidence_ids",
    ],
    "additionalProperties": False,
}

SUBMIT_VERDICT_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_verdict",
        "description": "Finish the investigation with an evidence-grounded adoption verdict.",
        "parameters": {
            "type": "object",
            "properties": {
                "rationale": {"type": "string"},
                "based_on_evidence_ids": {"type": "array", "items": {"type": "string"}},
                "verdict": VERDICT_SCHEMA,
            },
            "required": ["rationale", "based_on_evidence_ids", "verdict"],
            "additionalProperties": False,
        },
    },
}

REQUEST_BUDGET_TOOL = {
    "type": "function",
    "function": {
        "name": "request_more_budget",
        "description": "Pause for human approval when defined additional research may change the verdict.",
        "parameters": {
            "type": "object",
            "properties": {
                "rationale": {"type": "string"},
                "based_on_evidence_ids": {"type": "array", "items": {"type": "string"}},
                "summary_so_far": {"type": "string"},
                "provisional_verdict": VERDICT_SCHEMA,
                "unresolved_questions": {"type": "array", "items": {"type": "string"}},
                "proposed_next_checks": {"type": "array", "items": {"type": "string"}},
                "requested_calls": {"type": "integer", "minimum": 1, "maximum": 100},
                "expected_verdict_impact": {"type": "string"},
            },
            "required": [
                "rationale",
                "based_on_evidence_ids",
                "summary_so_far",
                "provisional_verdict",
                "unresolved_questions",
                "proposed_next_checks",
                "requested_calls",
                "expected_verdict_impact",
            ],
            "additionalProperties": False,
        },
    },
}


class InvestigationAgent:
    def __init__(
        self,
        store: InvestigationStore,
        llm: OpenAICompatibleClient,
        tools: GitHubToolRegistry,
        reports: ReportRenderer,
    ):
        self.store = store
        self.llm = llm
        self.tools = tools
        self.reports = reports

    def run(self, investigation_id: str) -> dict[str, Any]:
        while True:
            investigation = self.store.get_investigation(investigation_id)
            if investigation["status"] != InvestigationStatus.INVESTIGATING.value:
                return investigation

            remaining = investigation["remaining_budget"]
            if remaining <= 0:
                self._hard_pause(investigation)
                self.reports.render(investigation_id)
                return self.store.get_investigation(investigation_id)

            available_tools = [SUBMIT_VERDICT_TOOL, REQUEST_BUDGET_TOOL]
            if remaining > 1:
                available_tools = [*self.tools.definitions, *available_tools]

            call = self.store.reserve_llm_call(
                investigation_id, purpose="investigation", model=self.llm.model
            )
            try:
                response = self.llm.choose_tool(
                    messages=self._build_messages(investigation), tools=available_tools
                )
                self.store.complete_llm_call(
                    call["id"],
                    provider_request_id=response.provider_request_id,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                )
            except Exception as exc:
                self.store.fail_llm_call(call["id"], str(exc))
                self._record_invalid_step(
                    investigation,
                    call["ordinal"],
                    f"LLM call failed safely: {type(exc).__name__}: {exc}",
                )
                self.store.set_status(
                    investigation_id,
                    InvestigationStatus.PAUSED_EXTERNAL,
                    error=f"LLM provider failure: {exc}",
                )
                self.reports.render(investigation_id)
                return self.store.get_investigation(investigation_id)

            if not response.tool_calls:
                self._record_invalid_step(
                    investigation,
                    call["ordinal"],
                    "Expected exactly one function call, received none"
                    + (f"; prose: {response.content[:300]!r}" if response.content else ""),
                )
                continue

            # Providers with parallel tool calling may return several calls at once.
            # Executing the first keeps the one-action-per-call invariant without
            # burning a budgeted call on a retry; the discarded names are logged.
            action = response.tool_calls[0]
            if len(response.tool_calls) > 1:
                discarded = ", ".join(item.name for item in response.tool_calls[1:])
                print(
                    f"warning: model returned {len(response.tool_calls)} tool calls; "
                    f"executing {action.name} and discarding {discarded}",
                    file=sys.stderr,
                )
            try:
                outcome = self._handle_action(
                    investigation,
                    llm_call_number=call["ordinal"],
                    name=action.name,
                    arguments=action.arguments,
                )
            except DomainValidationError as exc:
                self._record_invalid_step(
                    investigation,
                    call["ordinal"],
                    f"Model action failed validation: {exc}",
                )
                continue

            if outcome in {"completed", "awaiting_budget"}:
                self.reports.render(investigation_id)
                return self.store.get_investigation(investigation_id)

    def _handle_action(
        self,
        investigation: dict[str, Any],
        *,
        llm_call_number: int,
        name: str,
        arguments: dict[str, Any],
    ) -> str:
        known_evidence = self.store.evidence_ids(investigation["id"])
        rationale = require_string(arguments, "rationale")
        based_on = require_string_list(arguments, "based_on_evidence_ids")
        unknown = sorted(set(based_on) - known_evidence)
        if unknown:
            raise DomainValidationError(
                f"action references unknown evidence IDs: {', '.join(unknown)}"
            )

        if name in self.tools.names:
            question = require_string(arguments, "question_to_answer")
            step_id = self.store.add_step(
                investigation["id"],
                revision=investigation["revision"],
                llm_call_number=llm_call_number,
                action_type="github_tool",
                rationale=rationale,
                question_to_answer=question,
                based_on_evidence_ids=based_on,
                tool_name=name,
                tool_arguments=arguments,
            )
            result = self.tools.execute(
                name,
                investigation=investigation,
                step_id=step_id,
                arguments=arguments,
            )
            self.store.complete_step(
                step_id,
                result_status=result.status.value,
                observation=result.summary,
                evidence_ids=result.evidence_ids,
            )
            return "continue"

        if name == "submit_verdict":
            verdict = normalize_verdict(arguments.get("verdict"), known_evidence)
            step_id = self.store.add_step(
                investigation["id"],
                revision=investigation["revision"],
                llm_call_number=llm_call_number,
                action_type="submit_verdict",
                rationale=rationale,
                question_to_answer=None,
                based_on_evidence_ids=based_on,
                tool_name=name,
                tool_arguments={"verdict": verdict},
            )
            self.store.complete_step(
                step_id,
                result_status=ToolResultStatus.SUCCESS.value,
                observation=f"Submitted verdict: {verdict['decision']}",
                evidence_ids=verdict["decisive_evidence_ids"],
            )
            self.store.complete_investigation(investigation["id"], verdict)
            return "completed"

        if name == "request_more_budget":
            requested_calls = arguments.get("requested_calls")
            if not isinstance(requested_calls, int) or not 1 <= requested_calls <= 100:
                raise DomainValidationError("requested_calls must be an integer from 1 to 100")
            provisional = normalize_verdict(
                arguments.get("provisional_verdict"), known_evidence
            )
            request = {
                "rationale": rationale,
                "based_on_evidence_ids": based_on,
                "summary_so_far": require_string(arguments, "summary_so_far"),
                "provisional_verdict": provisional,
                "unresolved_questions": require_string_list(
                    arguments, "unresolved_questions"
                ),
                "proposed_next_checks": require_string_list(
                    arguments, "proposed_next_checks"
                ),
                "requested_calls": requested_calls,
                "expected_verdict_impact": require_string(
                    arguments, "expected_verdict_impact"
                ),
            }
            step_id = self.store.add_step(
                investigation["id"],
                revision=investigation["revision"],
                llm_call_number=llm_call_number,
                action_type="request_more_budget",
                rationale=rationale,
                question_to_answer=None,
                based_on_evidence_ids=based_on,
                tool_name=name,
                tool_arguments={"requested_calls": requested_calls},
            )
            self.store.complete_step(
                step_id,
                result_status=ToolResultStatus.SUCCESS.value,
                observation=f"Paused and requested {requested_calls} additional LLM calls",
                evidence_ids=provisional["decisive_evidence_ids"],
            )
            self.store.request_budget(investigation["id"], request)
            return "awaiting_budget"

        raise DomainValidationError(f"unknown action tool: {name}")

    def _build_messages(self, investigation: dict[str, Any]) -> list[dict[str, str]]:
        evidence = self.store.list_evidence(investigation["id"])
        steps = self.store.list_steps(investigation["id"])
        retasks = [
            item["content"]
            for item in self.store.list_chat_messages(investigation["id"])
            if item["mode"] == "retask" and item["role"] == "user"
        ]

        ledger = []
        normalized_budget = 60_000
        for item in reversed(evidence):
            normalized_text = self._compact_json(item.get("normalized"), 6_000)
            if normalized_budget <= 0:
                normalized_text = "[details omitted; summary retained]"
            else:
                normalized_budget -= len(normalized_text)
            ledger.append(
                {
                    "id": item["id"],
                    "status": item["verification_status"],
                    "tool": item["tool_name"],
                    "summary": item["summary"],
                    "details": normalized_text,
                    "source_url": item.get("html_url") or item.get("api_url"),
                }
            )
        ledger.reverse()

        step_log = [
            {
                "step": item["sequence"],
                "action": item["tool_name"] or item["action_type"],
                "why": item["rationale"],
                "based_on": item["based_on_evidence_ids"],
                "result": item.get("observation"),
                "evidence": item["evidence_ids"],
            }
            for item in steps
        ]

        context = {
            "current_date_utc": current_date_utc(),
            "goal": investigation["goal"],
            "repository": investigation.get("canonical_full_name")
            or f"{investigation['owner']}/{investigation['repo']}",
            "intake": investigation.get("intake"),
            "user_retasks": retasks,
            "budget": {
                "used": investigation["investigation_calls_used"],
                "total": investigation["initial_budget"]
                + investigation["approved_extra_budget"],
                "remaining_before_this_call": investigation["remaining_budget"],
                "important": (
                    "If remaining is 1, only submit_verdict or request_more_budget is available."
                ),
            },
            "investigation_log": step_log,
            "evidence_ledger": ledger,
        }
        return [
            {"role": "system", "content": INVESTIGATOR_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "Choose the single highest-value next action from the available functions.\n\n"
                + json.dumps(context, ensure_ascii=False, indent=2),
            },
        ]

    @staticmethod
    def _compact_json(value: Any, limit: int) -> str:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        return text if len(text) <= limit else text[:limit] + "...[truncated]"

    def _record_invalid_step(
        self, investigation: dict[str, Any], llm_call_number: int, message: str
    ) -> None:
        step_id = self.store.add_step(
            investigation["id"],
            revision=investigation["revision"],
            llm_call_number=llm_call_number,
            action_type="invalid_model_action",
            rationale="The model response could not be executed safely.",
            question_to_answer=None,
            based_on_evidence_ids=[],
            tool_name=None,
            tool_arguments=None,
        )
        self.store.complete_step(
            step_id,
            result_status=ToolResultStatus.ERROR.value,
            observation=message,
            evidence_ids=[],
        )

    def _hard_pause(self, investigation: dict[str, Any]) -> None:
        request = {
            "rationale": "The hard LLM call budget was exhausted before a valid final control action.",
            "based_on_evidence_ids": sorted(self.store.evidence_ids(investigation["id"])),
            "summary_so_far": "The investigation is paused at the enforced budget boundary.",
            "provisional_verdict": investigation.get("verdict"),
            "unresolved_questions": ["A valid final verdict or budget request was not produced"],
            "proposed_next_checks": ["Allow the agent to synthesize the stored evidence"],
            "requested_calls": 2,
            "expected_verdict_impact": "Additional calls are needed to produce a validated final decision.",
        }
        self.store.request_budget(investigation["id"], request)

