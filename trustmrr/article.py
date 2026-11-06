"""TrustMRR deterministic report identity and private Markdown output."""
from __future__ import annotations



def money(cents):
    return "未提供" if cents is None else "US$" + format(cents / 100, ",.2f")


def percent(value):
    return "未提供" if value is None else format(value, ",.1f") + "%"


def title_for(period, monthly=False):
    period_date = period.split(":", 1)[1]
    if monthly:
        return "独立产品增长趋势｜" + period_date + " 四周观察"
    return "独立产品增长观察｜" + period_date[5:]


