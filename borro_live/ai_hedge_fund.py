"""ai-hedge-fund on Borro.

Two shapes, because the project has two:

* The portfolio manager's decisions, ``{ticker: {"action": "buy" | "sell" | "short" | "cover" | "hold",
  "quantity": shares, ...}}`` (what ``run_hedge_fund`` returns under ``"decisions"``): ``execute_decisions``.
* The v2 engine's ``Broker`` protocol (``positions()``, ``cash()``, ``place_order(order)`` with buy and sell orders
  on signed whole-share counts): ``BorroBroker``.

ai-hedge-fund itself is never imported unless it is installed: plain dicts and objects work.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from .broker import BorroLiveError, Broker, CapExceeded, Result, SimulationFailed, _dec
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
        super().__init__(f"dry run: {result.action} {result.symbol} was simulated ({'ok' if result.ok else 'it would fail'}) and not sent"
                         "; BorroBroker(broker, simulate_fills=True) returns simulated fills instead")
        self.result = result


class FillMismatch(BorroLiveError):
    """A live order was sent, but the wallet's book did not move by the order: the engine's ledger would not match."""


def _whole(shares: float) -> int:
    """The nearest whole share, halves up, so a book that gains n shares always reads n more."""
    return math.floor(shares + 0.5)


def _exact(pos: dict) -> tuple[dict, float]:
    """The wallet's book as the engine counts one: signed shares by ticker (not rounded), and cash.

    Shares are the stock posted under a long, less the stock a short owes. Cash is the USDG in the wallet, less the
    USDG owed under the longs, plus the USDG Borro holds against the shorts (the sale money and the deposit).
    """
    longs = {p.get("symbol") for p in pos["longs"]}
    shares, cash = {}, float(pos["usdg"] or 0)
    for s in pos["stocks"]:
        loan = (s.get("usdg_loan") or {}) if s.get("symbol") in longs else {}
        short = s.get("short") or {}
        n = float(loan.get("posted_stock") or 0) - float(short.get("owed_stock") or 0)
        cash += float(short.get("held_usdg") or 0) - float(loan.get("owed_usdg") or 0)
        if n:
            shares[s["symbol"]] = n
    return shares, cash


class BorroBroker:
    """ai-hedge-fund's v2 ``Broker`` protocol over a ``borro_live.Broker``.

    Orders there are whole shares on a signed book: a sell past zero is a short, a buy toward zero a cover. An order
    fills completely or raises. One that would cross zero, or cover part of a short, is refused (Borro closes a short
    whole), and so is a sell of less than 1% of a long.

    positions()   whole shares: the stock posted under each long, less the stock each short owes, rounded.
    cash()        USDG in the wallet, less the USDG owed under the longs, plus the USDG Borro holds against the shorts.
    place_order   a buy is sized from Borro's quote so the long gains the whole count. When the order has landed the
                  book is read again: if it did not move by exactly the order, ``FillMismatch`` is raised; otherwise
                  the ``Fill`` carries the price the cash really moved by, per share.

    A broker that is not live sends nothing. By default ``place_order`` then raises ``DryRun`` with Borro's simulation.
    With ``simulate_fills=True`` it returns a SIMULATED ``Fill`` instead and keeps those fills in a paper book in
    memory, so the engine can run whole sessions as a dry run: ``positions()`` and ``cash()`` add the paper book to
    what the wallet really holds. An opening order is priced from Borro's simulation of it from the wallet (a buy at
    the most the quote lets it cost). An order that closes shares held only on paper cannot be simulated by Borro and
    is priced at Borro's price; its ``Result`` says so in ``note``. Every ``Result`` is kept in ``results``.

    The caps are the underlying broker's: ``Broker(max_order_usdg=..., max_day_usdg=...)``.
    """

    def __init__(self, broker: Broker, *, simulate_fills: bool = False):
        if simulate_fills and broker.live:
            raise BorroLiveError("simulate_fills is for a dry run: this broker is live, and its fills are real")
        self.broker = broker
        self.simulate_fills = bool(simulate_fills)
        self.results: list[Result] = []
        self._paper: dict[str, int] = {}
        self._paper_cash = 0.0
        self._seen: tuple[float, dict] | None = None

    def _read(self, fresh: bool = False) -> dict:
        """The wallet's position, kept for a few seconds: the engine asks for positions and cash several times in a row."""
        if fresh or self._seen is None or time.monotonic() - self._seen[0] > 3:
            self._seen = (time.monotonic(), self.broker.positions())
        return self._seen[1]

    def exact(self) -> dict:
        """What the wallet holds on Borro right now, not rounded and without the paper book: ``{"shares", "cash"}``."""
        shares, cash = _exact(self._read(fresh=True))
        return {"shares": shares, "cash": cash}

    def positions(self) -> dict:
        Position, _ = _models()
        shares, _ = _exact(self._read())
        book = {}
        for symbol in set(shares) | set(self._paper):
            n = _whole(shares.get(symbol, 0)) + self._paper.get(symbol, 0)
            if n:
                book[symbol] = Position(ticker=symbol, shares=n)
        return book

    def cash(self) -> float:
        return _exact(self._read())[1] + self._paper_cash

    def place_order(self, order):
        _, Fill = _models()
        symbol, side, qty = str(_get(order, "ticker")).upper(), _get(order, "side"), int(_get(order, "quantity"))
        if side not in ("buy", "sell") or qty <= 0:
            raise BorroLiveError("an order is buy or sell of at least one share")
        price = self.broker.price(symbol)
        pos = self._read(fresh=True)
        shares, cash = _exact(pos)
        paper = self._paper.get(symbol, 0)
        long_, short_ = _long_shares(pos, symbol), _short_shares(pos, symbol)
        if paper:  # a dry run that already holds simulated shares of this stock: the book is the wallet's plus the paper
            net = _whole(long_ - short_) + paper
            long_, short_ = float(max(net, 0)), float(max(-net, 0))
        percent = None
        if side == "buy" and _whole(short_) > 0:
            if qty != _whole(short_):
                raise BorroLiveError(f"Borro closes a short whole: buy {_whole(short_)} {symbol} to cover it, then buy more")
            kind = "cover"
        elif side == "buy":
            kind = "buy"
        elif _whole(long_) > 0:
            if qty > _whole(long_):
                raise BorroLiveError(f"sell the {_whole(long_)} {symbol} held first, then short")
            kind, percent = "sell", 100.0 if qty == _whole(long_) else round(100.0 * qty / long_, 2)
            if percent < 1:
                raise BorroLiveError(f"Borro closes at least 1% of a long: selling {qty} of {_whole(long_)} {symbol} is under that")
        else:
            kind = "short"

        if paper and kind in ("sell", "cover"):
            res = Result(action=kind, symbol=symbol, usdg=None, live=False, sent=False, ok=False, tool="",
                         note=f"not simulated: these {symbol} shares are held only in this dry run's paper book, so the order is priced at Borro's price")
            fill_price = price
        else:
            try:
                res = self._send(kind, symbol, qty, price, percent)
            except CapExceeded as e:
                raise CapExceeded(f"{side} {qty} {symbol} is about {qty * price:,.2f} USDG: {e}. The engine orders whole shares, so set the caps to "
                                  "cover its orders: Broker(max_order_usdg=..., max_day_usdg=...), or BORRO_MAX_ORDER_USDG and BORRO_MAX_DAY_USDG") from None
            fill_price = None
        self.results.append(res)
        self._seen = None

        if res.sent:
            after_shares, after_cash = _exact(self._read(fresh=True))
            moved = _whole(after_shares.get(symbol, 0)) - _whole(shares.get(symbol, 0))
            paid = (cash - after_cash) if side == "buy" else (after_cash - cash)
            if moved != (qty if side == "buy" else -qty) or not paid > 0:
                raise FillMismatch(f"{side} {qty} {symbol} was sent ({', '.join(res.hashes)}), but the wallet's book moved by {moved:+d} shares and "
                                   f"{after_cash - cash:+.6f} USDG: it holds {after_shares.get(symbol, 0):g} {symbol} now")
            return Fill(ticker=symbol, side=side, quantity=qty, price=paid / qty)
        if not self.simulate_fills:
            raise DryRun(res)
        if fill_price is None:
            if not res.ok:
                raise SimulationFailed(f"{side} {qty} {symbol}: Borro's simulation failed, so there is no simulated fill")
            q = res.quote
            if kind == "buy":
                fill_price = (float(q["margin_usdg"]) + float(q["debt_usdg"])) / qty
            elif kind == "short":
                fill_price = float(q["after_fee_usdg"]) / qty
            elif kind == "sell":
                fill_price = (float(q["back_usdg"]) + float(q["owed_usdg"]) * percent / 100) / qty
            else:
                fill_price = price
                res.note = "simulated; Borro's quote for closing a short carries no cost, so the fill is priced at Borro's price"
        signed = qty if side == "buy" else -qty
        self._paper[symbol] = paper + signed
        self._paper_cash -= signed * fill_price
        return Fill(ticker=symbol, side=side, quantity=qty, price=fill_price)

    def _send(self, kind: str, symbol: str, qty: int, price: float, percent: float | None) -> Result:
        b = self.broker
        if kind == "cover":
            return b.cover(symbol)
        if kind == "sell":
            return b.sell(symbol, percent)
        b._check_caps(qty * price)
        if kind == "short":
            return b.short(symbol, qty * price)
        # Borro buys an exact amount of stock for a margin, and the amount grows with the margin: ask what a margin
        # near the order's worth buys, then scale it so the long gains the whole count.
        margin = qty * price / b.leverage
        quote = b.mcp.call("open_long", {"wallet": b.wallet, "symbol": symbol, "margin": _dec(margin, 6), "leverage": b.leverage})["quote"]
        return b.buy(symbol, margin * qty / float(quote["stock"]))
