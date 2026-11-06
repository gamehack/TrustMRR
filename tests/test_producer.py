"""Offline tests for independent GitHub Actions TrustMRR producer."""
import hashlib
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

from trustmrr.core import (
    normalize_top25, previous_week_changes, week_start,
)
from trustmrr.main import (
    report_identity, prior_ranking, month_candidate, produce,
)
from trustmrr.report_markdown import render_report_markdown


def sample_rows():
    return [{"slug": "product-" + str(i), "name": "Product " + str(i),
             "description": "A useful tool", "growth30d": 100-i,
             "revenue": {"last30Days": i * 101, "mrr": i * 23}}
            for i in range(1, 26)]


class NormalizeTests(unittest.TestCase):
    def test_only_fastest_growing_top25(self):
        rows = normalize_top25({
            "fastestGrowingStartups": sample_rows(),
            "recentlyAddedStartups": [{"slug": "ignore"}],
        })
        self.assertEqual(len(rows), 25)
        self.assertEqual(rows[0]["slug"], "product-1")
        self.assertEqual(rows[0]["rank"], 1)

    def test_rejects_missing_or_duplicate_top25(self):
        with self.assertRaises(ValueError):
            normalize_top25({"fastestGrowingStartups": sample_rows()[:24]})
        entries = sample_rows()
        entries[1]["slug"] = entries[0]["slug"]
        with self.assertRaises(ValueError):
            normalize_top25({"fastestGrowingStartups": entries})

    def test_no_bools_or_nan_as_amounts(self):
        items = sample_rows()
        items[0]["growth30d"] = True
        items[0]["revenue"]["mrr"] = "nan"
        result = normalize_top25({"fastestGrowingStartups": items})
        self.assertIsNone(result[0]["growth_pct"])
        self.assertIsNone(result[0]["mrr_cents"])

    def test_week_boundaries(self):
        self.assertEqual(week_start(date(2026, 11, 9)), date(2026, 11, 9))
        self.assertEqual(week_start(date(2026, 11, 11)), date(2026, 11, 9))
        self.assertTrue(month_candidate(date(2026, 12, 7)))
        self.assertFalse(month_candidate(date(2026, 11, 9)))
        self.assertEqual(report_identity("weekly", date(2026, 11, 9))[0],
                         "trustmrr-weekly:2026-11-09")
        self.assertEqual(report_identity("monthly", date(2026, 12, 7))[0],
                         "trustmrr-monthly:2026-12")


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.rows = normalize_top25({"fastestGrowingStartups": sample_rows()})
        self.research = {"overview": "原始研究，关注需求和风险",
                         "observations": ["观察一", "观察二", "观察三"]}

    def test_weekly_has_25_and_parseable_history(self):
        ranked = previous_week_changes(self.rows, [])
        markdown = render_report_markdown(
            period="weekly:2026-11-09",
            title="独立产品增长观察｜11-09", records=ranked, insights=self.research,
        )
        self.assertEqual(markdown.count("TrustMRR 来源"), 25)
        parsed = prior_ranking(markdown)
        self.assertEqual(len(parsed), 25)
        self.assertEqual(parsed[-1]["slug"], "product-25")
        self.assertEqual(parsed[-1]["rank"], 25)

    def test_monthly_report(self):
        rows = [{"slug": "product-1", "name": "Product 1", "appearances": 4}]
        markdown = render_report_markdown(
            period="monthly:2026-12", title="独立产品增长趋势｜2026-12 四周观察",
            records=rows, insights=self.research, monthly=True,
        )
        self.assertIn("近四期入榜 4 次", markdown)

    def test_rejects_invalid_insights(self):
        with self.assertRaises(ValueError):
            render_report_markdown(period="weekly:2026-11-09", title="title",
                records=self.rows, insights={"overview": "x", "observations": []})

    def test_untrusted_startup_name_escaped(self):
        rows = list(self.rows)
        rows[0] = dict(rows[0], name="**BAD** [hack](https://evil.example)")
        md = render_report_markdown(
            period="weekly:2026-11-09", title="Weekly", records=rows,
            insights=self.research)
        self.assertNotIn("**BAD** [hack]", md)


class OrchestrationTests(unittest.TestCase):
    @patch("trustmrr.main.read_existing_ready", return_value=None)
    @patch.dict("os.environ", {
        "TRUSTMRR_PRODUCTION_ENABLED": "false",
        "NOTION_PARENT_PAGE_ID": "4109e605-ccf8-81ed-a46c-f9ab95538f5b",
    })
    def test_inert_by_default(self, read):
        with self.assertRaises(PermissionError):
            produce(date(2026, 11, 9))
        read.assert_not_called()

    @patch("trustmrr.main.read_existing_ready", return_value=None)
    @patch.dict("os.environ", {
        "TRUSTMRR_PRODUCTION_ENABLED": "true",
        "TRUSTMRR_RESEARCH_AUTHORIZED": "false",
        "TRUSTMRR_AI_AUTHORIZED": "true",
        "NOTION_PARENT_PAGE_ID": "4109e605-ccf8-81ed-a46c-f9ab95538f5b",
    })
    def test_fails_closed_without_rights(self, read):
        with self.assertRaises(PermissionError):
            produce(date(2026, 11, 9))
        read.assert_not_called()

    def test_derived_weekly_not_revenue_growth(self):
        current = normalize_top25({"fastestGrowingStartups": sample_rows()})
        prior = [{"slug": "product-1", "rank": 2}]
        ranked = previous_week_changes(current, prior)
        self.assertEqual(ranked[0]["rank_delta"], 1)
        self.assertIsNone(ranked[1]["prior_rank"])


if __name__ == "__main__":
    unittest.main()
