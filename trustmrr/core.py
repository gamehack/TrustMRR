"""Strict TrustMRR Top-25 parsing and bounded history."""
from __future__ import annotations
import math
import re
from datetime import date, timedelta
from zoneinfo import ZoneInfo

DISCOVERY_URL = "https://trustmrr.com/api/ai/discovery"
SHANGHAI = ZoneInfo("Asia/Shanghai")
TOP_COUNT = 25

def number_or_none(value):
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None

def clean_text(value, limit=240):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]

def normalize_top25(payload):
    """Consume only fastestGrowingStartups. Ignore all other API groups."""
    if not isinstance(payload, dict):
        raise ValueError("Discovery API did not return a JSON object")
    rows = payload.get("fastestGrowingStartups")
    if not isinstance(rows, list) or len(rows) != TOP_COUNT:
        raise ValueError("Expected exactly 25 records in fastestGrowingStartups")
    result, seen = [], set()
    for rank, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError("Invalid startup entry")
        slug = str(row.get("slug") or "").strip()
        name = clean_text(row.get("name"), 100)
        if not re.fullmatch("[A-Za-z0-9-]{1,120}", slug) or not name:
            raise ValueError("Startup entry missing valid slug or name")
        if slug.lower() in seen:
            raise ValueError("Duplicate startup slug")
        seen.add(slug.lower())
        revenue = row.get("revenue") if isinstance(row.get("revenue"), dict) else {}
        result.append({
            "rank": rank, "slug": slug, "name": name,
            "description": clean_text(row.get("description") or row.get("tagline")),
            "growth_pct": number_or_none(row.get("growth30d")),
            "revenue_30d_cents": number_or_none(revenue.get("last30Days")),
            "mrr_cents": number_or_none(revenue.get("mrr")),
            "category": clean_text(row.get("category"), 60),
        })
    return result

def week_start(today: date) -> date:
    return today - timedelta(days=today.weekday())

def previous_week_changes(current, previous):
    earlier = {r["slug"]: r for r in previous}
    return [{
        **row,
        "prior_rank": earlier[row["slug"]]["rank"] if row["slug"] in earlier else None,
        "rank_delta": earlier[row["slug"]]["rank"] - row["rank"]
        if row["slug"] in earlier else None,
    } for row in current]


# Stateless GitHub Actions runs source their comparison history from Notion READY,
# never from local SQLite, filesystem cache, or GitHub public artifacts.
