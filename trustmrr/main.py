"""Weekly TrustMRR research runner for stateless GitHub Actions.

Canonical Notion READY pages are the only historical store. The runner never
creates WeChat or CMS drafts and does not maintain an on-runner SQLite database.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .article import title_for
from .core import (
    DISCOVERY_URL, SHANGHAI, normalize_top25, previous_week_changes,
    week_start,
)
from .notion_sync import check_parent_access, read_existing_ready, sync_article
from .report_markdown import render_report_markdown
from .services import ai_insights, authorize, enabled, http_json

PARENT_ID = "4109e605-ccf8-81ed-a46c-f9ab95538f5b"
SOURCE_RE = re.compile(r"https://trustmrr\.com/startup/([A-Za-z0-9-]+)")
HEADING_RE = re.compile(r"^(\d{1,2})\.\s+(.+)$")


def report_identity(kind: str, monday: date) -> tuple[str, str, str]:
    if monday.weekday() != 0:
        raise ValueError("Period must be a Monday")
    label, external = (
        ("trustmrr-weekly", monday.isoformat())
        if kind == "weekly" else ("trustmrr-monthly", monday.strftime("%Y-%m"))
    )
    prefix = external + ("｜TrustMRR 周报｜" if kind == "weekly" else "｜TrustMRR 月报｜")
    title = prefix + title_for(
        ("weekly:" if kind == "weekly" else "monthly:") + external,
        monthly=(kind == "monthly"),
    )
    return label + ":" + external, prefix, title


def prior_ranking(markdown: str) -> list[dict]:
    """Extract the canonical TrustMRR source URLs from an existing READY article.

    No JSON cache is committed to the public repository. Only exact 25/25 rows
    are accepted, otherwise monthly statistics are not manufactured.
    """
    if "## TrustMRR 增长 Top 25" not in markdown:
        raise ValueError("Weekly Notion report lacks Top 25")
    seen, result, rank, name = set(), [], None, ""
    in_list = False
    for line in markdown.splitlines():
        if line.strip() == "## TrustMRR 增长 Top 25":
            in_list = True
            continue
        if in_list and line.startswith("## "):
            break
        if not in_list:
            continue
        match = HEADING_RE.match(line)
        if match:
            rank = int(match.group(1))
            name = re.sub(r"<[^>]+>|[*_\\]", "", match.group(2)).strip()
            continue
        matched = SOURCE_RE.search(line)
        if matched and rank is not None:
            slug = matched.group(1)
            if rank < 1 or rank > 25 or slug in seen:
                raise ValueError("Invalid rank or duplicate slug in Notion history")
            seen.add(slug)
            result.append({"rank": rank, "slug": slug, "name": name[:100] or slug})
            rank = None
    if len(result) != 25 or [r["rank"] for r in result] != list(range(1, 26)):
        raise ValueError("Historical Notion Top25 is incomplete")
    return result


def historical_week(monday: date) -> list[dict] | None:
    key, prefix, _ = report_identity("weekly", monday)
    try:
        markdown = read_existing_ready(key, prefix)
    except RuntimeError as exc:
        # Legacy Notion pages (including 2026-11-02) predate the READY marker.
        # Treat them as unavailable comparison history, never as verified Top25.
        # An existing current-period page is still handled strictly in produce().
        if str(exc) == "Existing report period lacks correct READY; manual reconciliation required":
            return None
        raise
    return None if markdown is None else prior_ranking(markdown)


def write_period(kind: str, monday: date, rows: list[dict], comparison: dict) -> str:
    key, prefix, title = report_identity(kind, monday)
    existing = read_existing_ready(key, prefix)
    if existing is not None:
        return "ALREADY_READY " + key
    insights = ai_insights(rows, comparison, kind)
    markdown = render_report_markdown(
        period=("weekly:" if kind == "weekly" else "monthly:")
        + (monday.isoformat() if kind == "weekly" else monday.strftime("%Y-%m")),
        title=title_for(
            ("weekly:" if kind == "weekly" else "monthly:")
            + (monday.isoformat() if kind == "weekly" else monday.strftime("%Y-%m")),
            monthly=(kind == "monthly"),
        ),
        records=rows, insights=insights, monthly=(kind == "monthly"),
    )
    result = sync_article(
        source_key=key, title=title, unique_prefix=prefix, markdown=markdown,
    )
    return str(result["result"]) + " " + key


def month_candidate(monday: date) -> bool:
    return monday.weekday() == 0 and monday.day <= 7


def produce(monday: date) -> list[str]:
    if monday.weekday() != 0:
        raise ValueError("TrustMRR weekly production requires a Monday")
    if not enabled("TRUSTMRR_PRODUCTION_ENABLED"):
        raise PermissionError("TRUSTMRR_PRODUCTION_ENABLED must explicitly be true")
    authorize("TRUSTMRR_RESEARCH_AUTHORIZED", "TRUSTMRR_AI_AUTHORIZED")
    if not enabled("NOTION_ENABLE_SYNC") or not enabled("NOTION_AUTOSYNC_AFTER_GENERATION"):
        raise PermissionError("Notion READY handoff is not enabled")
    parent_id = os.getenv("NOTION_PARENT_PAGE_ID", "")
    if parent_id.lower() != PARENT_ID:
        raise ValueError("Unexpected Notion parent page")
    week_key, week_prefix, _ = report_identity("weekly", monday)
    existing = read_existing_ready(week_key, week_prefix)
    current = None
    results = []
    if existing is not None:
        current = prior_ranking(existing)
        results.append("ALREADY_READY " + week_key)
    else:
        previous = historical_week(monday - timedelta(days=7))
        payload = http_json(DISCOVERY_URL)
        current = normalize_top25(payload)
        ranked = previous_week_changes(current, previous or [])
        results.append(write_period("weekly", monday, ranked, {
            "has_prior_week": bool(previous),
            "rank_delta": "prior position minus current position, not revenue growth",
        }))
        # Monthly ranking must come from Notion's confirmed weekly artifact.
        current = historical_week(monday)
    if not month_candidate(monday):
        return results
    snapshots = [current]
    for offset in (1, 2, 3):
        earlier = historical_week(monday - timedelta(days=7 * offset))
        if earlier is None:
            results.append("MONTHLY_SKIPPED insufficient consecutive READY history")
            return results
        snapshots.append(earlier)
    if any(not week or len(week) != 25 for week in snapshots):
        raise ValueError("Invalid monthly weekly history")
    frequencies: dict[str, dict] = {}
    for weekly_rows in snapshots:
        for row in weekly_rows:
            item = frequencies.setdefault(
                row["slug"], {"name": row["name"], "slug": row["slug"], "appearances": 0}
            )
            item["appearances"] += 1
    monthly_rows = sorted(
        frequencies.values(), key=lambda r: (-r["appearances"], r["name"])
    )[:10]
    results.append(write_period("monthly", monday, monthly_rows, {
        "period": "four consecutive weekly Top25 snapshots",
        "note": "appearance frequency is not growth or profit",
    }))
    return results


def write_test() -> dict[str, str]:
    """Write one clearly labeled, idempotent Notion integration-test page (v3)."""
    source_key = "trustmrr-test-v3:2026-11-07"
    unique_prefix = "联调测试｜2026-11-07-v3｜"
    title = unique_prefix + "Notion 写入与 READY 回读验证"
    markdown = (
        "# Notion 写入与 READY 回读验证\n\n"
        "本页由 TrustMRR GitHub Actions 的一次性联调任务创建。\n\n"
        "- 测试范围：Notion 子页面创建、标题与父页面校验、Markdown 全文回读、SHA-256 校验、READY 标记写入及最终回读。\n"
        "- 本页不是正式周报，不会被 TrustMRR 周报发布器或下游公众号/CMS 发布器识别。\n"
        "- 本页使用独立测试唯一键；重复执行会校验现有 READY 内容，不会覆盖页面。\n"
    )
    return sync_article(
        source_key=source_key,
        title=title,
        unique_prefix=unique_prefix,
        markdown=markdown,
    )


def integration_test() -> dict[str, object]:
    """Run real TrustMRR -> OmniRoute -> report render -> Notion READY end to end (isolated test; v1)."""
    source_key = "trustmrr-e2e-test:2026-11-07"
    unique_prefix = "联调测试｜2026-11-07-e2e｜"
    title = unique_prefix + "真实 Top25 与 AI 研究完整链路"
    if os.getenv("NOTION_PARENT_PAGE_ID", "").lower() != PARENT_ID:
        raise ValueError("Unexpected Notion parent page")
    if not enabled("NOTION_ENABLE_SYNC"):
        raise PermissionError("NOTION_ENABLE_SYNC must explicitly be true")
    authorize("TRUSTMRR_RESEARCH_AUTHORIZED", "TRUSTMRR_AI_AUTHORIZED")
    existing = read_existing_ready(source_key, unique_prefix)
    if existing is not None:
        result = sync_article(
            source_key=source_key, title=title, unique_prefix=unique_prefix,
            markdown=existing,
        )
        return {"result": result["result"], "source_key": source_key,
                "page_id": result["page_id"], "page_url": result["page_url"],
                "top25": "already_ready", "observations": "already_ready"}

    payload = http_json(DISCOVERY_URL)
    rows = normalize_top25(payload)
    insights = ai_insights(rows, {
        "mode": "live end-to-end integration test; no previous READY ranking",
        "rank_delta": "not calculated for this test",
    }, "weekly")
    target_period = "weekly:2026-11-09"
    markdown = render_report_markdown(
        period=target_period,
        title=title_for(target_period, monthly=False),
        records=previous_week_changes(rows, []),
        insights=insights,
        monthly=False,
    )
    result = sync_article(
        source_key=source_key, title=title, unique_prefix=unique_prefix,
        markdown=markdown,
    )
    return {"result": result["result"], "source_key": source_key,
            "page_id": result["page_id"], "page_url": result["page_url"],
            "top25": len(rows), "observations": len(insights["observations"]),
            "markdown_chars": len(markdown)}


def preflight(target_monday: date | None = None) -> dict:
    """Exercise live TrustMRR, OmniRoute and Notion-read paths without writing."""
    authorize("TRUSTMRR_RESEARCH_AUTHORIZED", "TRUSTMRR_AI_AUTHORIZED")
    if os.getenv("NOTION_PARENT_PAGE_ID", "").lower() != PARENT_ID:
        raise ValueError("Unexpected Notion parent page")
    notion_status = check_parent_access()
    payload = http_json(DISCOVERY_URL)
    rows = normalize_top25(payload)
    insights = ai_insights(rows, {
        "mode": "live preflight; no previous-week comparison; no report will be archived",
        "rank_delta": "not calculated in preflight",
    }, "weekly")
    if target_monday is None:
        target_monday = week_start(datetime.now(SHANGHAI).date() + timedelta(days=7))
    if target_monday.weekday() != 0:
        raise ValueError("Preflight target period must be a Monday")
    period = "weekly:" + target_monday.isoformat()
    ranked = previous_week_changes(rows, [])
    markdown = render_report_markdown(
        period=period,
        title=title_for(period, monthly=False),
        records=ranked,
        insights=insights,
        monthly=False,
    )
    if markdown.count("TrustMRR 来源") != 25:
        raise ValueError("Preflight report rendering did not produce exactly 25 sources")
    return {
        "notion_read": bool(notion_status.get("readable")),
        "trustmrr_top25": len(rows),
        "ai_observations": len(insights["observations"]),
        "markdown_chars": len(markdown),
        "target_period": target_monday.isoformat(),
        "notion_write": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TrustMRR GitHub Actions Notion producer")
    parser.add_argument(
        "command", choices=("check", "preflight", "write-test", "integration-test", "produce"),
        help="check verifies Notion read access; preflight tests live APIs without writes; write-test verifies Notion READY writes; integration-test runs the full live chain; produce writes reports",
    )
    args = parser.parse_args(argv)
    try:
        local_today = datetime.now(SHANGHAI).date()
        monday = week_start(local_today)
        if args.command == "check":
            if os.getenv("NOTION_PARENT_PAGE_ID", "").lower() != PARENT_ID:
                raise ValueError("Unexpected Notion parent page")
            status = check_parent_access()
            print("CHECK_OK Notion parent readable", "sampled_children=" + str(status["sampled_children"]))
            return 0
        if args.command == "preflight":
            result = preflight()
            print("PREFLIGHT_OK", "notion_read=ok", "trustmrr_top25=" + str(result["trustmrr_top25"]),
                  "ai_observations=" + str(result["ai_observations"]),
                  "markdown_chars=" + str(result["markdown_chars"]),
                  "target_period=" + result["target_period"], "notion_write=not_attempted")
            return 0
        if args.command == "write-test":
            if os.getenv("NOTION_PARENT_PAGE_ID", "").lower() != PARENT_ID:
                raise ValueError("Unexpected Notion parent page")
            if not enabled("NOTION_ENABLE_SYNC"):
                raise PermissionError("NOTION_ENABLE_SYNC must explicitly be true")
            result = write_test()
            print("WRITE_TEST_OK", result["result"], "source_key=" + result["source_key"],
                  "page_id=" + result["page_id"], "page_url=" + result["page_url"])
            return 0
        if args.command == "integration-test":
            result = integration_test()
            print("E2E_TEST_OK", result["result"], "source_key=" + str(result["source_key"]),
                  "top25=" + str(result["top25"]), "observations=" + str(result["observations"]),
                  "page_id=" + str(result["page_id"]), "page_url=" + str(result["page_url"]))
            return 0
        if local_today.weekday() != 0:
            raise ValueError("Production date is not a Beijing Monday; no backfill")
        for line in produce(monday):
            print(line)
        return 0
    except (ValueError, PermissionError, RuntimeError) as exc:
        print("ERROR", type(exc).__name__, str(exc).replace("\n", " ")[:240],
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
