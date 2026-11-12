"""ai-hedge-fund on Borro.

Two shapes, because the project has two:

* The portfolio manager's decisions, ``{ticker: {"action": "buy" | "sell" | "short" | "cover" | "hold",
  "quantity": shares, ...}}`` (what ``run_hedge_fund`` returns under ``"decisions"``): ``execute_decisions``.
* The v2 engine's ``Broker`` protocol (``positions()``, ``cash()``, ``place_order(order)`` with buy and sell orders
  on signed share counts): ``BorroBroker``.

ai-hedge-fund itself is never imported unless it is installed: plain dicts and objects work.
"""

from __future__ import annotations

from dataclasses import dataclass

from .broker import BorroLiveError, Broker, Result
from .chain import ChainError
from .mcp import McpError

ACTIONS = ("buy", "sell", "short", "cover", "hold")


def _get(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _long_shares(pos: dict, symbol: str) -> float:
    if not any(p.get("symbol") == symbol for p in pos["longs"]):
        return 0.0
    row = next((s for s in pos["stocks"] if s.get("symbol") == symbol), {})
    return float((row.get("usdg_loan") or {}).get("posted_stock") or 0)


def _short_shares(pos: dict, symbol: str) -> float:
    return float(next((s.get("owed_stock") or 0 for s in pos["shorts"] if s.get("symbol") == symbol), 0))


def execute_decisions(broker: Broker, decisions, scale: float = 1.0) -> dict:
    """Execute the portfolio manager's decisions, ticker by ticker.

    ``decisions`` is the dict of decisions, the whole ``run_hedge_fund`` result, or the manager's output object.
    ``scale`` multiplies every quantity (0.01 turns 10 shares into 0.1), since the model sizes for a paper
    portfolio and the caps here are in USDG. Returns ``{ticker: Result}``; a ticker this kit or Borro refused maps
    to the reason as a string, and a hold to None, so one refusal never stops the others.

    buy     buy ``quantity`` shares' worth        sell    sell that share of the long
    short   short ``quantity`` shares' worth      cover   close the short (Borro closes a short whole)
    """
    inner = _get(decisions, "decisions")
    decisions = inner if inner is not None else decisions
    if not isinstance(decisions, dict):
        raise BorroLiveError("decisions must be a dict of ticker to {action, quantity}")
    out: dict = {}
    pos = None
    for ticker, d in decisions.items():
        symbol = str(ticker).strip().upper()
        action = str(_get(d, "action", "hold")).strip().lower()
        qty = float(_get(d, "quantity", 0) or 0) * scale
        try:
            if action not in ACTIONS:
                raise BorroLiveError(f"unknown action {action}")
            if action == "hold" or (qty <= 0 and action != "cover"):
                out[symbol] = None
            elif action == "buy":
                out[symbol] = broker.buy(symbol, qty * broker.price(symbol))
            elif action == "short":
                out[symbol] = broker.short(symbol, qty * broker.price(symbol))
            elif action == "cover":
                out[symbol] = broker.cover(symbol)
            else:
                broker.price(symbol)
                pos = pos or broker.positions()
                held = _long_shares(pos, symbol)
                if held <= 0:
                    raise BorroLiveError(f"no long on {symbol} to sell")
                out[symbol] = broker.sell(symbol, max(1.0, min(100.0, 100.0 * qty / held)))
        except (BorroLiveError, McpError, ChainError) as e:
            out[symbol] = f"refused: {e}"
    return out


@dataclass
class _Position:
    ticker: str
    shares: int


@dataclass
class _Fill:
    ticker: str
    side: str
    quantity: int
    price: float


def _models():
    try:
        from hedge_fund.brokers.models import Fill, Position  # the real ones when ai-hedge-fund is installed
        return Position, Fill
    except Exception:
        return _Position, _Fill


class DryRun(BorroLiveError):
    """place_order on a broker that is not live: nothing was sent. ``result`` holds Borro's simulation."""

    def __init__(self, result: Result):
        super().__init__(f"dry run: {result.action} {result.symbol} was simulated ({'ok' if result.ok else 'it would fail'}) and not sent")
        self.result = result


class BorroBroker:
    """ai-hedge-fund's v2 ``Broker`` protocol over a live ``borro_live.Broker``.

    Orders there are whole shares on a signed book: a sell past zero is a short, a buy toward zero a cover. On Borro
    a buy is a 1.01x long and its share count is what the swap gave, so ``positions()`` rounds to whole shares. An
    order fills completely or raises: one that would cross zero, or cover part of a short, is refused (Borro closes
    a short whole). A dry-run broker raises ``DryRun`` with the simulation, since nothing was filled.
    """

    def __init__(self, broker: Broker):
        self.broker = broker
        self.results: list[Result] = []

    def positions(self) -> dict:
        Position, _ = _models()
        pos = self.broker.positions()
        book = {}
        for symbol in {p["symbol"] for p in pos["longs"]} | {p["symbol"] for p in pos["shorts"]}:
            shares = round(_long_shares(pos, symbol) - _short_shares(pos, symbol))
            if shares:
                book[symbol] = Position(ticker=symbol, shares=shares)
        return book

    def cash(self) -> float:
        return float(self.broker.positions()["usdg"] or 0)

    def place_order(self, order):
        _, Fill = _models()
        symbol, side, qty, ref = str(_get(order, "ticker")).upper(), _get(order, "side"), int(_get(order, "quantity")), float(_get(order, "price"))
        if side not in ("buy", "sell") or qty <= 0:
            raise BorroLiveError("an order is buy or sell of at least one share")
        pos = self.broker.positions()
        long_, short_ = _long_shares(pos, symbol), _short_shares(pos, symbol)
        price = self.broker.price(symbol)
        if side == "buy" and short_ > 0:
            if qty != round(short_):
                raise BorroLiveError(f"Borro closes a short whole: buy {round(short_)} {symbol} to cover it, then buy more")
            res = self.broker.cover(symbol)
        elif side == "buy":
            res = self.broker.buy(symbol, qty * price)
        elif long_ > 0:
            if qty > round(long_):
                raise BorroLiveError(f"sell the {round(long_)} {symbol} held first, then short")
            res = self.broker.sell(symbol, min(100.0, 100.0 * qty / long_))
        else:
            res = self.broker.short(symbol, qty * price)
        self.results.append(res)
        if not res.sent:
            raise DryRun(res)
        q = res.quote
        filled = float(q["size_usdg"]) / float(q["stock"]) if q.get("size_usdg") and q.get("stock") else float(q.get("price_usd") or price or ref)
        return Fill(ticker=symbol, side=side, quantity=qty, price=filled)
