from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import Settings
from .storage import InvestigationStore


class ReportRenderer:
    """Deterministic Markdown rendering. No LLM is used here."""

    def __init__(self, store: InvestigationStore, settings: Settings):
        self.store = store
        self.settings = settings

    def render(self, investigation_id: str) -> Path:
        investigation = self.store.get_investigation(investigation_id)
        steps = self.store.list_steps(investigation_id)
        evidence = self.store.list_evidence(investigation_id)
        verdict = investigation.get("verdict")
        intake = investigation.get("intake") or {}

        title = investigation.get("canonical_full_name") or f"{investigation['owner']}/{investigation['repo']}"
        lines = [
            f"# Repo Detective Report: {title}",
            "",
            f"- **Investigation ID:** `{investigation_id}`",
            f"- **Status:** `{investigation['status']}`",
            f"- **Revision:** {investigation['revision']}",
            f"- **Repository:** {investigation.get('html_url') or investigation['input_url']}",
            f"- **Goal:** {investigation['goal']}",
            f"- **LLM investigation calls:** {investigation['investigation_calls_used']} / "
            f"{investigation['initial_budget'] + investigation['approved_extra_budget']}",
            f"- **Remaining investigation calls:** {investigation['remaining_budget']}",
            "",
        ]

        if verdict:
            lines.extend(
                [
                    "## Verdict",
                    "",
                    f"**{verdict.get('decision', 'unavailable').upper()}** "
                    f"(confidence: {verdict.get('confidence', 'unknown')})",
                    "",
                    str(verdict.get("executive_summary", "")),
                    "",
                ]
            )
            self._render_claims(lines, "Positive signals", verdict.get("positive_signals", []))
            self._render_claims(lines, "Risk factors", verdict.get("risk_factors", []))
            self._render_list(lines, "Adoption conditions", verdict.get("adoption_conditions", []))
            self._render_list(lines, "Unverified items", verdict.get("unverified_items", []))
        else:
            lines.extend(["## Verdict", "", "No verdict has been submitted yet.", ""])

        pending = investigation.get("pending_budget_request")
        if pending:
            lines.extend(
                [
                    "## Human approval required",
                    "",
                    str(pending.get("summary_so_far", "The investigation needs more budget.")),
                    "",
                    f"**Requested calls:** {pending.get('requested_calls')}",
                    "",
                    f"**Why it may matter:** {pending.get('expected_verdict_impact', '')}",
                    "",
                ]
            )
            self._render_list(lines, "Unresolved questions", pending.get("unresolved_questions", []))
            self._render_list(lines, "Proposed checks", pending.get("proposed_next_checks", []))

        lines.extend(["## Intake snapshot", ""])
        if intake:
            lines.extend(
                [
                    f"- Description: {intake.get('description') or 'Not provided'}",
                    f"- Stars: {intake.get('stars', 'unknown')}",
                    f"- Forks: {intake.get('forks', 'unknown')}",
                    f"- Archived: {intake.get('archived', 'unknown')}",
                    f"- Default branch: {intake.get('default_branch', 'unknown')}",
                    f"- Last push: {intake.get('pushed_at', 'unknown')}",
                    f"- Latest release: {(intake.get('latest_release') or {}).get('tag_name', 'not verified/found')}",
                    "",
                ]
            )
        else:
            lines.extend(["Intake did not complete.", ""])

        lines.extend(["## Investigation log", ""])
        if not steps:
            lines.extend(["No agent steps were recorded.", ""])
        for step in steps:
            lines.extend(
                [
                    f"### Step {step['sequence']} - {step['action_type']}",
                    "",
                    f"- **Why:** {step['rationale']}",
                    f"- **Question:** {step.get('question_to_answer') or 'N/A'}",
                    f"- **Based on:** {', '.join(step['based_on_evidence_ids']) or 'system state'}",
                    f"- **Action:** {step.get('tool_name') or step['action_type']}",
                    f"- **Result:** {step.get('observation') or 'No result recorded'}",
                    f"- **Evidence:** {', '.join(step['evidence_ids']) or 'none'}",
                    "",
                ]
            )

        lines.extend(["## Evidence", ""])
        if evidence:
            lines.extend(
                [
                    "| ID | Tool | Status | Summary | Source |",
                    "|---|---|---|---|---|",
                ]
            )
            for item in evidence:
                source = item.get("html_url") or item.get("api_url")
                summary = self._table_text(item.get("summary", ""))
                lines.append(
                    f"| `{item['id']}` | `{item['tool_name']}` | "
                    f"`{item['verification_status']}` | {summary} | [link]({source}) |"
                )
            lines.append("")
        else:
            lines.extend(["No evidence was collected.", ""])

        lines.extend(
            [
                "## Method note",
                "",
                "This report was rendered deterministically from the stored verdict, "
                "investigation log, and GitHub API evidence. No LLM call was used to write it.",
                "",
            ]
        )

        path = self.settings.reports_dir / f"{investigation_id}-r{investigation['revision']}.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    @staticmethod
    def _render_claims(lines: list[str], title: str, claims: list[dict[str, Any]]) -> None:
        if not claims:
            return
        lines.extend([f"### {title}", ""])
        for claim in claims:
            refs = ", ".join(f"`{item}`" for item in claim.get("evidence_ids", []))
            lines.append(f"- {claim.get('statement', '')} ({claim.get('claim_type', 'observed')}; {refs})")
        lines.append("")

    @staticmethod
    def _render_list(lines: list[str], title: str, items: list[str]) -> None:
        if not items:
            return
        lines.extend([f"### {title}", ""])
        lines.extend(f"- {item}" for item in items)
        lines.append("")

    @staticmethod
    def _table_text(value: str) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")[:500]

