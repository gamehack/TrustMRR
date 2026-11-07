"""Offline tests for independent GitHub Actions TrustMRR producer."""
import hashlib
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

from trustmrr.core import (
    normalize_top25, previous_week_changes, week_start,
)
from trustmrr.main import (
    report_identity, prior_ranking, month_candidate, produce, historical_week, preflight, write_test, integration_test,
)
from trustmrr.report_markdown import render_report_markdown
from trustmrr.notion_sync import _comparison_form


def sample_rows():
    return [{"slug": "product-" + str(i), "name": "Product " + str(i),
             "description": "A useful tool", "growth30d": 100-i,
             "revenue": {"last30Days": i * 101, "mrr": i * 23}}
            for i in range(1, 26)]


class NotionMarkdownTests(unittest.TestCase):
    def test_markdown_v2_bold_autolinks_and_dollar_escapes_are_equivalent(self):
        expected = "1. **ECOMSTATEMENT\\.COM**\\n   - Price US\\$2.00\\n"
        actual = "1. [<b>ECOMSTATEMENT.COM</b>](http://ECOMSTATEMENT.COM)\\n   - Price US\\$2.00\\n"
        self.assertEqual(_comparison_form(expected), _comparison_form(actual))
    def test_markdown_v2_code_tags_and_escaped_underscores_are_equivalent(self):
        expected = "> Archive key: `trustmrr-test:2026-11-07`.\n\n- source_key\n"
        actual = "> Archive key: <code>trustmrr-test:2026-11-07</code>.\n\n- source\\_key\n"
        self.assertEqual(_comparison_form(expected), _comparison_form(actual))


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

    def test_two_digit_rank_bullets_keep_nested_indentation(self):
        rows = normalize_top25({"fastestGrowingStartups": sample_rows()})
        markdown = render_report_markdown(
            period="weekly:2026-11-09", title="TrustMRR weekly",
            records=previous_week_changes(rows, []),
            insights={"overview": "概述", "observations": ["一", "二", "三"]},
        )
        lines = markdown.splitlines()
        rank_ten = lines.index("10. **Product 10**")
        self.assertTrue(lines[rank_ten + 1].startswith("    - "))
        self.assertTrue(lines[rank_ten + 2].startswith("    - "))
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


class NetworkSafetyTests(unittest.TestCase):
    @patch.dict("os.environ", {
        "TRUSTMRR_AI_AUTHORIZED": "true",
        "OMNIROUTE_BASE_URL": "http://invalid.example",
        "OMNIROUTE_API_KEY": "dummy",
        "OMNIROUTE_MODEL": "dummy-model",
    })
    def test_model_credentials_refuse_plain_http(self):
        from trustmrr.services import ai_insights
        with self.assertRaises(ValueError):
            ai_insights([], {}, "weekly")


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

    @patch("trustmrr.main.read_existing_ready", side_effect=RuntimeError(
        "Existing report period lacks correct READY; manual reconciliation required"))
    def test_legacy_history_without_ready_is_skipped(self, read):
        self.assertIsNone(historical_week(date(2026, 11, 2)))
        read.assert_called_once()

    @patch("trustmrr.main.read_existing_ready", side_effect=RuntimeError(
        "Historical Notion READY digest mismatch"))
    def test_tampered_history_is_not_trusted(self, read):
        with self.assertRaises(RuntimeError):
            historical_week(date(2026, 11, 2))

    def test_live_preflight_runs_three_integrations_without_writing(self):
        from unittest.mock import patch
        import os
        env = {
            "TRUSTMRR_RESEARCH_AUTHORIZED": "true",
            "TRUSTMRR_AI_AUTHORIZED": "true",
            "NOTION_API_KEY": "secret-test-token-that-is-long-enough",
            "NOTION_PARENT_PAGE_ID": "4109e605-ccf8-81ed-a46c-f9ab95538f5b",
        }
        with patch.dict(os.environ, env):
            with patch("trustmrr.main.check_parent_access", return_value={"readable": True, "sampled_children": 1}) as notion, patch("trustmrr.main.http_json", return_value={"fastestGrowingStartups": sample_rows()}) as api, patch("trustmrr.main.ai_insights", return_value={"overview": "概述", "observations": ["一", "二", "三"]}) as model, patch("trustmrr.main.sync_article") as writer:
                result = preflight(date(2026, 11, 9))
        self.assertEqual(result["trustmrr_top25"], 25)
        self.assertEqual(result["target_period"], "2026-11-09")
        self.assertFalse(result["notion_write"])
        notion.assert_called_once()
        api.assert_called_once()
        model.assert_called_once()
        writer.assert_not_called()

    @patch("trustmrr.main.sync_article", return_value={
        "result": "created_ready", "source_key": "trustmrr-test-v3:2026-11-07",
        "page_id": "00000000-0000-4000-8000-000000000001",
        "page_url": "https://www.notion.so/test",
    })
    def test_write_test_uses_dedicated_idempotent_test_identity(self, sync):
        result = write_test()
        self.assertEqual(result["source_key"], "trustmrr-test-v3:2026-11-07")
        self.assertTrue(sync.call_args.kwargs["title"].startswith("联调测试｜2026-11-07-v3｜"))
        self.assertTrue(sync.call_args.kwargs["unique_prefix"].startswith("联调测试｜"))
        self.assertIn("不是正式周报", sync.call_args.kwargs["markdown"])

    @patch("trustmrr.main.sync_article", return_value={
        "result": "created_ready", "source_key": "trustmrr-e2e-test-v2:2026-11-07",
        "page_id": "00000000-0000-4000-8000-000000000002",
        "page_url": "https://www.notion.so/e2e-test",
    })
    @patch("trustmrr.main.read_existing_ready", return_value=None)
    @patch("trustmrr.main.ai_insights", return_value={
        "overview": "真实链路测试概述", "observations": ["观察一", "观察二", "观察三"],
    })
    @patch("trustmrr.main.http_json", return_value={"fastestGrowingStartups": sample_rows()})
    @patch.dict("os.environ", {
        "TRUSTMRR_RESEARCH_AUTHORIZED": "true",
        "TRUSTMRR_AI_AUTHORIZED": "true",
        "NOTION_ENABLE_SYNC": "true",
        "NOTION_API_KEY": "secret-test-token-that-is-long-enough",
        "NOTION_PARENT_PAGE_ID": "4109e605-ccf8-81ed-a46c-f9ab95538f5b",
    })
    def test_full_integration_test_combines_live_sources_and_notion_write(
        self, api, model, existing, writer
    ):
        result = integration_test()
        self.assertEqual(result["top25"], 25)
        self.assertEqual(result["observations"], 3)
        self.assertEqual(result["source_key"], "trustmrr-e2e-test-v2:2026-11-07")
        self.assertIn("TrustMRR 增长 Top 25", writer.call_args.kwargs["markdown"])
        api.assert_called_once()
        model.assert_called_once()
        existing.assert_called_once()
        writer.assert_called_once()

    def test_derived_weekly_not_revenue_growth(self):
        current = normalize_top25({"fastestGrowingStartups": sample_rows()})
        prior = [{"slug": "product-1", "rank": 2}]
        ranked = previous_week_changes(current, prior)
        self.assertEqual(ranked[0]["rank_delta"], 1)
        self.assertIsNone(ranked[1]["prior_rank"])


if __name__ == "__main__":
    unittest.main()
