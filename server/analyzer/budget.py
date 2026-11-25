"""Per-org daily extraction budget: the spend cap the ETL checks before every drain.

Two caps, USD and tokens, each off at 0. Tokens count input plus output only; cached reads are
excluded because gateways bill them at a fraction and the OpenAI path does not report them
consistently.
"""
from dataclasses import dataclass

from sqlalchemy import func, select

from analyzer import db, models, tenant


@dataclass(frozen=True)
class Spend:
    """Today's ledger against the org's caps. `over` names the cap that tripped, or None."""
    over: str | None
    usd: float
    usd_cap: float
    tokens: int
    tokens_cap: int


def over_budget(con):
    """Today's spend for the org whose corpus `con` is: the file is the org, so the sum needs no org
    column. Never raises: a broken ledger must not block the queue. date('now') stays SQL-side."""
    cfg = tenant.config(con)
    usd_cap, tokens_cap = cfg.daily_usd, cfg.daily_tokens
    if usd_cap <= 0 and tokens_cap <= 0:
        return Spend(None, 0.0, usd_cap, 0, tokens_cap)
    c = models.costs.c
    try:
        usd, tokens = db.run(con, select(func.ifnull(func.sum(c.cost_usd), 0),
                                         func.ifnull(func.sum(c.input_tokens), 0)
                                         + func.ifnull(func.sum(c.output_tokens), 0))
                             .where(c.at >= func.date("now"))).fetchone()
    except Exception:
        return Spend(None, 0.0, usd_cap, 0, tokens_cap)
    usd, tokens = float(usd or 0), int(tokens or 0)
    over = "usd" if 0 < usd_cap <= usd else "tokens" if 0 < tokens_cap <= tokens else None
    return Spend(over, usd, usd_cap, tokens, tokens_cap)
