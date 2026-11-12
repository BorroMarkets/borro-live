"""TradingAgents on Borro: turn a run's rating into an order.

    from borro_live import Broker
    from borro_live.tradingagents import go_live

    ta = go_live(TradingAgentsGraph(config=config), Broker.from_env(), usdg=5)
    _, decision = ta.propagate("NVDA", "2026-09-01")      # the same answer as before; ta.borro_orders has the orders

``TradingAgentsGraph.propagate(ticker, date)`` returns ``(final_state, signal)``; the signal is one of Buy,
Overweight, Hold, Underweight, Sell, or REVIEW when the run reached no rating. Older versions answer BUY, SELL or
HOLD. TradingAgents itself is never imported here: a plain string, the tuple or the final state all work.
"""

from __future__ import annotations

import re
import warnings

from .broker import BorroLiveError, Broker, Result
from .chain import ChainError
from .mcp import McpError

RATINGS = ("buy", "overweight", "hold", "underweight", "sell")


def rating_of(decision) -> str | None:
    """The rating in a decision, lower case, or None when there is none to act on (REVIEW included)."""
    if isinstance(decision, (tuple, list)) and decision:
        decision = decision[-1]
    if isinstance(decision, dict):
        decision = decision.get("final_rating") or decision.get("signal") or decision.get("decision") or ""
    word = re.sub(r"[^a-z]", "", str(decision or "").lower())
    return word if word in RATINGS else None


def execute(broker: Broker, ticker: str, decision, usdg: float = 5.0, allow_short: bool = False) -> list[Result]:
    """Act on one decision for one ticker. Returns the orders made (none for Hold, REVIEW or nothing to sell).

    Buy          cover a short if there is one, then buy ``usdg`` worth
    Overweight   the same, half the size
    Hold         nothing
    Underweight  sell half of the long, if there is one
    Sell         sell all of the long; with ``allow_short`` and no long, short ``usdg`` worth instead
    """
    rating = rating_of(decision)
    if rating in (None, "hold"):
        return []
    symbol = str(ticker).strip().upper()
    broker.price(symbol)  # refuses a ticker Borro does not list, before anything else
    pos = broker.positions()
    has_long = any(p.get("symbol") == symbol for p in pos["longs"])
    has_short = any(p.get("symbol") == symbol for p in pos["shorts"])
    out: list[Result] = []
    if rating in ("buy", "overweight"):
        if has_short:
            out.append(broker.cover(symbol))
        out.append(broker.buy(symbol, usdg if rating == "buy" else usdg / 2))
    elif has_long:
        out.append(broker.sell(symbol, 100 if rating == "sell" else 50))
    elif rating == "sell" and allow_short and not has_short:
        out.append(broker.short(symbol, usdg))
    return out


def go_live(graph, broker: Broker, usdg: float = 5.0, allow_short: bool = False):
    """Wrap ``graph.propagate`` so every run's decision is executed on Borro. Returns the same graph.

    ``propagate`` answers exactly as before. The orders are in ``graph.borro_orders``; if Borro or this kit refused
    (a cap, an unlisted ticker, a failed simulation), the reason is in ``graph.borro_error`` and a warning is raised.
    """
    inner = graph.propagate

    def propagate(company_name, trade_date, *args, **kwargs):
        result = inner(company_name, trade_date, *args, **kwargs)
        graph.borro_orders, graph.borro_error = [], None
        try:
            graph.borro_orders = execute(broker, company_name, result, usdg=usdg, allow_short=allow_short)
        except (BorroLiveError, McpError, ChainError) as e:
            graph.borro_error = e
            warnings.warn(f"borro-live did not complete the order for {company_name}: {e}", stacklevel=2)
        return result

    graph.propagate = propagate
    return graph
