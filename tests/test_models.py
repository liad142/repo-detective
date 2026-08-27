from __future__ import annotations

import unittest

from repo_detective.models import DomainValidationError, normalize_verdict


class VerdictValidationTests(unittest.TestCase):
    def valid_verdict(self) -> dict:
        return {
            "decision": "adopt_with_conditions",
            "confidence": "medium",
            "executive_summary": "The project is usable with an explicit maintenance fallback.",
            "positive_signals": [
                {
                    "statement": "Recent commits were observed.",
                    "evidence_ids": ["EV-1"],
                    "claim_type": "observed",
                }
            ],
            "risk_factors": [
                {
                    "statement": "Contribution is concentrated.",
                    "evidence_ids": ["EV-2"],
                    "claim_type": "inference",
                }
            ],
            "adoption_conditions": ["Pin a reviewed release"],
            "unverified_items": ["Private maintainer succession plans are unknown"],
            "decisive_evidence_ids": ["EV-1", "EV-2"],
        }

    def test_accepts_grounded_verdict(self) -> None:
        result = normalize_verdict(self.valid_verdict(), {"EV-1", "EV-2"})
        self.assertEqual(result["decision"], "adopt_with_conditions")

    def test_rejects_unknown_evidence(self) -> None:
        with self.assertRaisesRegex(DomainValidationError, "unknown evidence"):
            normalize_verdict(self.valid_verdict(), {"EV-1"})

    def test_conditions_are_required(self) -> None:
        verdict = self.valid_verdict()
        verdict["adoption_conditions"] = []
        with self.assertRaisesRegex(DomainValidationError, "requires at least one condition"):
            normalize_verdict(verdict, {"EV-1", "EV-2"})


if __name__ == "__main__":
    unittest.main()

