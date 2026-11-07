"""Pure TrustMRR report Markdown generation and period identity validation.

No channel credentials, HTTP clients, or channel submission functions.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from urllib.parse import quote

from .article import money, percent

MARKDOWN_LIMIT = 180_000
REPORT_MARKDOWN_LIMIT = MARKDOWN_LIMIT
REPORT_SOURCE_WEEKLY = "trustmrr-weekly"
REPORT_SOURCE_MONTHLY = "trustmrr-monthly"
_MARKDOWN_SPECIAL = re.compile(r"([\\\x60*_{}\[\]()#+.!|<>~-])")
_WEEKLY_PERIOD = re.compile(r"^weekly:(\d{4}-\d{2}-\d{2})$")
_MONTHLY_PERIOD = re.compile(r"^monthly:(\d{4}-\d{2})$")


def _period_identity(period: str) -> tuple[str, str]:
    if not isinstance(period, str):
        raise ValueError("Report period must be a string")
    weekly = _WEEKLY_PERIOD.fullmatch(period)
    if weekly:
        raw_date = weekly.group(1)
        try:
            parsed = date.fromisoformat(raw_date)
        except ValueError:
            raise ValueError("Invalid weekly Report period") from None
        if parsed.isoformat() != raw_date or parsed.weekday() != 0:
            raise ValueError("Weekly Report period must be a canonical Monday date")
        return REPORT_SOURCE_WEEKLY, raw_date

    monthly = _MONTHLY_PERIOD.fullmatch(period)
    if monthly:
        raw_month = monthly.group(1)
        try:
            parsed = datetime.strptime(raw_month, "%Y-%m")
        except ValueError:
            raise ValueError("Invalid monthly Report period") from None
        if parsed.strftime("%Y-%m") != raw_month:
            raise ValueError("Invalid monthly Report period")
        return REPORT_SOURCE_MONTHLY, raw_month

    raise ValueError("Report period must be weekly:YYYY-MM-DD or monthly:YYYY-MM")


def _escape_markdown(value: object) -> str:
    normalized = str(value or "").replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    return _MARKDOWN_SPECIAL.sub(r"\\\1", normalized)


def render_report_markdown(
    *,
    period: str,
    title: str,
    records: list[dict],
    insights: dict,
    monthly: bool = False,
) -> str:
    """Render report data deterministically; never ask the model to generate HTML/Markdown."""
    source, _ = _period_identity(period)
    expected_source = REPORT_SOURCE_MONTHLY if monthly else REPORT_SOURCE_WEEKLY
    if source != expected_source:
        raise ValueError("Report period type does not match report type")
    if not isinstance(records, list) or not records:
        raise ValueError("Report records are required")
    if not isinstance(insights, dict):
        raise ValueError("Report insights are required")
    overview = insights.get("overview")
    observations = insights.get("observations")
    if not isinstance(overview, str) or not overview.strip():
        raise ValueError("Report overview is required")
    if not isinstance(observations, list) or not 3 <= len(observations) <= 5:
        raise ValueError("Report must contain 3 to 5 observations")
    if not all(isinstance(item, str) and item.strip() for item in observations):
        raise ValueError("Report observations must be non-empty strings")

    lines = [f"# {_escape_markdown(title)}", "", _escape_markdown(overview), "", "## 研究观察", ""]
    for observation in observations:
        lines.append(f"- {_escape_markdown(observation)}")

    if monthly:
        lines.extend(["", "## 近四期持续上榜产品", ""])
        for row in records:
            if not isinstance(row, dict):
                raise ValueError("Invalid monthly report row")
            name = _escape_markdown(row.get("name", ""))
            appearances = row.get("appearances")
            if not isinstance(appearances, int) or isinstance(appearances, bool) or not 1 <= appearances <= 4:
                raise ValueError("Invalid monthly appearance count")
            slug = str(row.get("slug") or "")
            source_url = "https://trustmrr.com/startup/" + quote(slug, safe="")
            lines.append(
                f"- **{name}** — 近四期入榜 {appearances} 次。"
                f" [TrustMRR 来源]({source_url})"
            )
    else:
        lines.extend(["", "## TrustMRR 增长 Top 25", ""])
        for row in records:
            if not isinstance(row, dict):
                raise ValueError("Invalid weekly report row")
            rank = row.get("rank")
            if not isinstance(rank, int) or isinstance(rank, bool) or not 1 <= rank <= 25:
                raise ValueError("Invalid weekly rank")
            name = _escape_markdown(row.get("name", ""))
            slug = str(row.get("slug") or "")
            growth = _escape_markdown(percent(row.get("growth_pct")))
            revenue = _escape_markdown(money(row.get("revenue_30d_cents")))
            mrr = _escape_markdown(money(row.get("mrr_cents")))
            prior_rank = row.get("prior_rank")
            if prior_rank is None:
                change = "上周未出现在 Top 25"
            else:
                if not isinstance(prior_rank, int) or isinstance(prior_rank, bool) or not 1 <= prior_rank <= 25:
                    raise ValueError("Invalid prior weekly rank")
                delta = prior_rank - rank
                change = f"上周第 {prior_rank} 位；排名变化 {delta:+d}"
            description = row.get("description")
            nested_indent = " " * (len(str(rank)) + 2)
            lines.extend([
                f"{rank}. **{name}**",
                f"{nested_indent}- 近30日收入增长：{growth}；近30日收入：{revenue}；MRR：{mrr}",
                f"{nested_indent}- {change}",
            ])
            if description:
                lines.append(f"{nested_indent}- {_escape_markdown(description)}")
            lines.append(f"{nested_indent}- [TrustMRR 来源](https://trustmrr.com/startup/{quote(slug, safe='')})")

    lines.extend([
        "",
        "---",
        "",
        "数据来源：TrustMRR 官方公开发现接口。增长率属于近30日收入口径，不是本周增长率；"
        "排名变化不能等同收入涨跌。数据仅反映平台覆盖的产品，收入/增长按来源披露，"
        "未提供的信息不作推断。本文仅讨论一般性市场趋势，不构成投资建议，也不建议复制或克隆个别产品。",
        "",
    ])
    result = "\n".join(lines).strip() + "\n"
    if len(result) > REPORT_MARKDOWN_LIMIT:
        raise ValueError("Report Markdown body exceeds limit")
    return result


