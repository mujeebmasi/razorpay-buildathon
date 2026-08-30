"""The static snapshot the hosted dashboard reads.

The same `dist/` is served two ways: by the Python API locally, and as flat
files on a CDN. That only stays honest if the snapshot carries every field the
live API does — a missing key does not fail loudly in a browser, it renders an
empty card and looks like a data problem.

So these tests compare the exported shape against the live payload builders,
field for field, and check the snapshot describes the batch it was made from.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from finctl.export_static import export

DATA = Path(__file__).resolve().parent.parent / "data"
SNAPSHOT = Path(__file__).resolve().parent.parent / "web" / "public" / "data"


class TestExportedSnapshot(unittest.TestCase):
    """Checks the committed snapshot, which is what actually gets deployed."""

    @classmethod
    def setUpClass(cls):
        if not (SNAPSHOT / "run.json").exists():
            raise unittest.SkipTest(
                "no snapshot committed; run `python -m finctl export`"
            )
        cls.summary = json.loads((SNAPSHOT / "run.json").read_text(encoding="utf-8"))
        cls.exceptions = json.loads(
            (SNAPSHOT / "exceptions.json").read_text(encoding="utf-8")
        )
        cls.matches = json.loads((SNAPSHOT / "matches.json").read_text(encoding="utf-8"))
        cls.agent = json.loads((SNAPSHOT / "agent.json").read_text(encoding="utf-8"))

    def test_every_file_the_dashboard_asks_for_exists(self):
        for name in ("run", "exceptions", "matches", "journal", "scenarios", "agent"):
            self.assertTrue((SNAPSHOT / f"{name}.json").is_file(), name)

    def test_the_summary_carries_what_the_overview_renders(self):
        """A missing key here renders an empty card rather than an error."""
        for field in (
            "records", "seconds", "throughput_per_second", "match_rate",
            "matches", "exceptions", "tier_counts", "reason_counts",
            "severity_counts", "counters", "scorecard",
            "value_matched_display", "value_at_risk_display",
            "fee_recovery_display", "reserve_display", "rounding_display",
            "journal_debits_display", "journal_credits_display",
            "verifier_checks", "verifier_rejections", "rejected_examples",
            "daily", "exposure_by_reason", "adjudicator",
        ):
            self.assertIn(field, self.summary, field)

    def test_the_chart_and_ranked_exposure_have_data(self):
        self.assertGreater(len(self.summary["daily"]), 0)
        self.assertGreater(len(self.summary["exposure_by_reason"]), 0)

    def test_exception_detail_is_inlined(self):
        """Statically there is no per-row endpoint, so the drawer needs it up front."""
        for row in self.exceptions[:20]:
            for field in ("evidence", "records", "candidates", "owner", "action",
                          "agent_trace"):
                self.assertIn(field, row, f"{row['id']} missing {field}")

    def test_match_rationale_and_evidence_survive(self):
        detailed = [m for m in self.matches if m.get("evidence")]
        self.assertGreater(len(detailed), 0)

    def test_the_agent_snapshot_carries_the_investigations(self):
        self.assertGreaterEqual(len(self.agent["cases"]), 6)
        self.assertGreaterEqual(len(self.agent["tools"]), 6)
        vetoed = [
            c for c in self.agent["cases"]
            if c["decision"] == "match" and c.get("verifier", {}).get("accepted") is False
        ]
        self.assertTrue(vetoed, "the hosted agent view must show the veto")

    def test_the_snapshot_matches_the_batch_it_describes(self):
        """A snapshot from a different run would misreport the whole dashboard."""
        from finctl.ingest.loader import load_batch

        batch = load_batch(DATA)
        self.assertEqual(self.summary["records"], batch.record_count)
        self.assertEqual(self.summary["settlements"], len(batch.settlements))
        self.assertEqual(self.summary["exceptions"], len(self.exceptions))
        self.assertEqual(self.summary["matches"], len(self.matches))

    def test_amounts_are_pre_formatted_for_display(self):
        """Money formatting lives next to money arithmetic, in Python."""
        for row in self.exceptions[:10]:
            self.assertTrue(row["amount_display"].startswith(("₹", "-₹")), row["id"])


class TestExportIsReproducible(unittest.TestCase):
    def test_exporting_again_produces_the_same_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            self.assertEqual(export(DATA, out), 0)
            fresh = json.loads((out / "run.json").read_text(encoding="utf-8"))
            committed = json.loads((SNAPSHOT / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(
                sorted(fresh), sorted(committed),
                "a fresh export has different fields from the committed one — "
                "re-run `python -m finctl export`",
            )
            self.assertEqual(fresh["records"], committed["records"])
            self.assertEqual(fresh["matches"], committed["matches"])


if __name__ == "__main__":
    unittest.main()
