from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def json_dumps(value: Any) -> str:
    def default(obj: Any) -> Any:
        if isinstance(obj, StrEnum):
            return obj.value
        if is_dataclass(obj):
            return asdict(obj)
        raise TypeError(f"Cannot JSON serialize {type(obj)!r}")

    return json.dumps(value, default=default, ensure_ascii=False, sort_keys=True)


def json_loads(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value)


class InvestigationStatus(StrEnum):
    CREATED = "created"
    INTAKE_RUNNING = "intake_running"
    INVESTIGATING = "investigating"
    AWAITING_BUDGET = "awaiting_budget"
    COMPLETED = "completed"
    PAUSED_EXTERNAL = "paused_external"
    INTAKE_FAILED = "intake_failed"


class VerificationStatus(StrEnum):
    VERIFIED = "verified"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"


class ToolResultStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    ERROR = "error"


class VerdictDecision(StrEnum):
    ADOPT = "adopt"
    ADOPT_WITH_CONDITIONS = "adopt_with_conditions"
    REJECT = "reject"


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(slots=True)
class RepositoryRef:
    input_url: str
    owner: str
    name: str
    github_id: int | None = None
    canonical_full_name: str | None = None
    html_url: str | None = None


@dataclass(slots=True)
class ToolResult:
    status: ToolResultStatus
    summary: str
    facts: dict[str, Any] = field(default_factory=dict)
    evidence_ids: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    has_more: bool = False
    next_page: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class LLMToolCall:
    name: str
    arguments: dict[str, Any]
    call_id: str | None = None


@dataclass(slots=True)
class LLMResponse:
    tool_calls: list[LLMToolCall]
    content: str | None
    provider_request_id: str | None
    input_tokens: int | None
    output_tokens: int | None
    raw: dict[str, Any]


class DomainValidationError(ValueError):
    """A model-produced object violated a domain invariant."""


class BudgetExhausted(RuntimeError):
    """No investigation LLM calls remain."""


class ExternalServiceError(RuntimeError):
    """An external provider returned an unusable response."""


def require_string(data: dict[str, Any], key: str, *, max_length: int = 4_000) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DomainValidationError(f"{key} must be a non-empty string")
    value = value.strip()
    if len(value) > max_length:
        raise DomainValidationError(f"{key} exceeds {max_length} characters")
    return value


def require_string_list(
    data: dict[str, Any], key: str, *, allow_empty: bool = False, max_items: int = 100
) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise DomainValidationError(f"{key} must be a list of strings")
    cleaned = [item.strip() for item in value if item.strip()]
    if not allow_empty and not cleaned:
        raise DomainValidationError(f"{key} must not be empty")
    if len(cleaned) > max_items:
        raise DomainValidationError(f"{key} exceeds {max_items} items")
    return cleaned


def normalize_claims(value: Any, field_name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise DomainValidationError(f"{field_name} must be a list")
    claims: list[dict[str, Any]] = []
    for index, claim in enumerate(value):
        if not isinstance(claim, dict):
            raise DomainValidationError(f"{field_name}[{index}] must be an object")
        statement = require_string(claim, "statement", max_length=2_000)
        evidence_ids = require_string_list(claim, "evidence_ids")
        claim_type = claim.get("claim_type", "observed")
        if claim_type not in {"observed", "inference"}:
            raise DomainValidationError(
                f"{field_name}[{index}].claim_type must be observed or inference"
            )
        claims.append(
            {
                "statement": statement,
                "evidence_ids": evidence_ids,
                "claim_type": claim_type,
            }
        )
    return claims


def normalize_verdict(data: Any, known_evidence_ids: set[str]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise DomainValidationError("verdict must be an object")

    decision = data.get("decision")
    if decision not in {item.value for item in VerdictDecision}:
        raise DomainValidationError("decision must be adopt, adopt_with_conditions, or reject")

    confidence = data.get("confidence")
    if confidence not in {item.value for item in Confidence}:
        raise DomainValidationError("confidence must be low, medium, or high")

    positive_signals = normalize_claims(data.get("positive_signals", []), "positive_signals")
    risk_factors = normalize_claims(data.get("risk_factors", []), "risk_factors")
    conditions = require_string_list(
        data, "adoption_conditions", allow_empty=True, max_items=30
    )
    decisive = require_string_list(data, "decisive_evidence_ids")

    unknowns_value = data.get("unverified_items", [])
    if not isinstance(unknowns_value, list) or any(
        not isinstance(item, str) for item in unknowns_value
    ):
        raise DomainValidationError("unverified_items must be a list of strings")
    unknowns = [item.strip() for item in unknowns_value if item.strip()]

    all_refs = set(decisive)
    for claim in positive_signals + risk_factors:
        all_refs.update(claim["evidence_ids"])
    unknown_refs = sorted(all_refs - known_evidence_ids)
    if unknown_refs:
        raise DomainValidationError(
            f"verdict references unknown evidence IDs: {', '.join(unknown_refs)}"
        )

    if decision == VerdictDecision.ADOPT_WITH_CONDITIONS and not conditions:
        raise DomainValidationError("adopt_with_conditions requires at least one condition")
    if decision in {VerdictDecision.ADOPT, VerdictDecision.ADOPT_WITH_CONDITIONS} and not positive_signals:
        raise DomainValidationError("an adoption verdict requires at least one positive signal")
    if decision == VerdictDecision.REJECT and not risk_factors:
        raise DomainValidationError("reject requires at least one risk factor")

    return {
        "decision": decision,
        "confidence": confidence,
        "executive_summary": require_string(data, "executive_summary", max_length=4_000),
        "positive_signals": positive_signals,
        "risk_factors": risk_factors,
        "adoption_conditions": conditions,
        "unverified_items": unknowns,
        "decisive_evidence_ids": decisive,
    }
