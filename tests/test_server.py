"""The dashboard server: routing, payload shape, and the static boundary.

The frontend is a separate React/TypeScript build, which introduces a way for
the project to break that the engine tests would never notice: the API can be
perfect while the page fails to load, or the committed build can drift away
from the payload the UI expects. These tests pin the seam between them.
"""
from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

from server.app import STATIC_DIR, Handler, _State

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


class ServerTestCase(unittest.TestCase):
    """Boots the real server on an ephemeral port and talks to it over HTTP."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.state = _State(DATA, "local")
        cls.state.refresh()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, state=cls.state))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def fetch(self, path: str) -> tuple[int, bytes, str]:
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, response.read(), response.headers.get_content_type()
        except urllib.error.HTTPError as exc:
            # HTTPError is itself a response object and holds a socket, so it
            # has to be closed explicitly or the test run leaks warnings.
            with exc:
                return exc.code, exc.read(), exc.headers.get_content_type()

    def json(self, path: str) -> dict:
        status, body, _ = self.fetch(path)
        self.assertEqual(status, 200, path)
        return json.loads(body)


class TestStaticBoundary(ServerTestCase):
    def test_the_committed_build_is_present(self):
        """The build ships in the repo so a clone runs with no Node installed."""
        self.assertTrue(
            (STATIC_DIR / "index.html").is_file(),
            f"the dashboard build is missing from {STATIC_DIR}. Run: "
            f"cd web && npm install && npm run build",
        )

    def test_root_serves_the_dashboard(self):
        status, body, content_type = self.fetch("/")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "text/html")
        self.assertIn(b'id="root"', body)

    def test_the_page_references_a_bundle_that_exists(self):
        """A stale index.html pointing at a deleted bundle would render blank."""
        _, body, _ = self.fetch("/")
        page = body.decode("utf-8")
        for marker, path in (('src="', ".js"), ('href="', ".css")):
            for chunk in page.split(marker)[1:]:
                asset = chunk.split('"')[0]
                if asset.startswith("/assets/") and asset.endswith(path):
                    status, payload, _ = self.fetch(asset)
                    self.assertEqual(status, 200, asset)
                    self.assertGreater(len(payload), 0, asset)

    def test_path_traversal_is_refused(self):
        """A crafted asset path must not read files outside the build."""
        for attack in (
            "/assets/../../finctl/money.py",
            "/assets/../../../etc/passwd",
            "/static/../server/app.py",
        ):
            status, _, _ = self.fetch(attack)
            self.assertEqual(status, 404, attack)

    def test_unknown_routes_return_json_not_html(self):
        status, body, _ = self.fetch("/api/nope")
        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(body))


class TestApiContract(ServerTestCase):
    """The fields the TypeScript types in `web/src/types.ts` declare.

    A missing field here surfaces in the browser as `undefined` rendered into
    the page, which is exactly the class of failure a typed client is supposed
    to prevent -- but TypeScript can only check the shape it was told about, so
    the runtime side is asserted here.
    """

    def test_run_payload_has_every_declared_field(self):
        run = self.json("/api/run")
        for field in (
            "records", "seconds", "throughput_per_second", "matches", "exceptions",
            "match_rate", "value_matched_display", "value_at_risk_display",
            "reserve_display", "fee_recovery_display", "tier_counts", "reason_counts",
            "counters", "adjudicator", "adjudicated", "adjudicator_abstained",
            "verifier_checks", "verifier_rejections", "verifier_rejected_adjudications",
            "rejected_examples", "journal_entries", "journal_balances",
            "daily", "exposure_by_reason",
        ):
            self.assertIn(field, run, f"/api/run is missing {field!r}")

    def test_scorecard_is_present_when_labels_exist(self):
        card = self.json("/api/run").get("scorecard")
        self.assertIsNotNone(card)
        for field in (
            "accuracy", "match_precision", "match_recall", "exception_recall",
            "reason_accuracy", "false_match_rate", "auto_resolve_rate", "total_cases",
        ):
            self.assertIn(field, card)

    def test_daily_series_is_chart_ready(self):
        for point in self.json("/api/run")["daily"]:
            self.assertIn("date", point)
            self.assertIsInstance(point["matched"], int)
            self.assertIsInstance(point["broken"], int)
            self.assertTrue(point["matched_display"])

    def test_exposure_is_ranked_by_money(self):
        rows = self.json("/api/run")["exposure_by_reason"]
        amounts = [row["amount"] for row in rows]
        self.assertEqual(amounts, sorted(amounts, reverse=True))

    def test_exception_paging_is_stable_and_complete(self):
        first = self.json("/api/exceptions?limit=25&offset=0")
        self.assertEqual(first["returned"], 25)
        self.assertTrue(first["has_more"])

        second = self.json("/api/exceptions?limit=25&offset=25")
        self.assertEqual(second["offset"], 25)

        # Pages must not overlap, or the UI would render duplicate rows as it
        # appends each page to the list it already has.
        overlap = {i["id"] for i in first["items"]} & {i["id"] for i in second["items"]}
        self.assertEqual(overlap, set())

    def test_paging_walks_the_whole_register(self):
        seen: set[str] = set()
        offset = 0
        while True:
            page = self.json(f"/api/exceptions?limit=100&offset={offset}")
            seen.update(item["id"] for item in page["items"])
            if not page["has_more"]:
                break
            offset += page["returned"]
        self.assertEqual(len(seen), page["total"])

    def test_filters_narrow_the_register(self):
        everything = self.json("/api/exceptions?limit=1")["total"]
        critical = self.json("/api/exceptions?limit=1&severity=critical")["total"]
        self.assertLess(critical, everything)
        self.assertGreater(critical, 0)

    def test_every_exception_row_carries_what_the_ui_renders(self):
        for item in self.json("/api/exceptions?limit=40")["items"]:
            for field in (
                "id", "reason", "severity", "amount_display", "as_of",
                "summary", "owner", "action", "candidate_count",
            ):
                self.assertIn(field, item)
            self.assertIn(
                item["severity"], {"critical", "high", "medium", "low", "info"}
            )

    def test_exception_detail_includes_the_evidence_trail(self):
        first = self.json("/api/exceptions?limit=1")["items"][0]
        detail = self.json(f"/api/exception/{first['id']}")
        self.assertEqual(detail["id"], first["id"])
        self.assertIn("evidence", detail)
        self.assertIn("records", detail)

    def test_unknown_exception_id_is_reported_not_crashed(self):
        self.assertIn("error", self.json("/api/exception/does_not_exist"))

    def test_journal_and_scenarios_render_ready(self):
        journal = self.json("/api/journal?limit=3")
        self.assertIn("trial_balance", journal)
        self.assertTrue(journal["balanced"])

        scenarios = self.json("/api/scenarios")["scenarios"]
        self.assertGreaterEqual(len(scenarios), 25)
        for row in scenarios:
            self.assertIn(
                row["difficulty"], {"trivial", "routine", "hard", "unresolvable"}
            )
            self.assertLessEqual(row["correct"], row["cases"])


if __name__ == "__main__":
    unittest.main()
