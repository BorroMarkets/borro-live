"""The broker: buy, sell, short, cover, lend and positions on Borro, with the safety rules in one place.

buy and sell are plain spot orders: USDG for a stock token in the wallet, and back. short, cover and lend use Borro's
lending market.

Nothing is sent unless the broker was made with ``live=True`` AND the environment has ``BORRO_LIVE=1``. Without
both, every order is a dry run: it returns Borro's own simulation of each step and sends nothing.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

from .chain import CHAIN_ID, Chain, ChainError, account_from_key, calldata
from .mcp import BorroLiveError, McpClient, ToolRefused

USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
# Spot orders go through Uniswap: the token is approved to Permit2, Permit2 lets the Universal Router take it, and the
# router swaps. A spot order's steps may go nowhere else.
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
UNIVERSAL_ROUTER = "0x8876789976dEcBfCbBbe364623C63652db8C0904"
PERMIT2_APPROVE = "0x87517c45"  # approve(address token, address spender, uint160 amount, uint48 expiration)
SPOT_TOOLS = ("buy_stock", "sell_stock")
# BorroBuilders: deposits made through it with a registered code are credited to that code (Bookrunners).
BORRO_BUILDERS = "0x082Ae609bdC7ccEec0C58B2361a21c49dc2aE291"
APPROVE = "0x095ea7b3"

DEFAULT_MAX_ORDER_USDG = 10.0
DEFAULT_MAX_DAY_USDG = 25.0
# short() deposits this much USDG for each USDG of stock sold, on top of the sale money Borro holds.
DEFAULT_SHORT_DEPOSIT = 1.0


class UnlistedTicker(BorroLiveError):
    pass


class CapExceeded(BorroLiveError):
    pass


class SimulationFailed(BorroLiveError):
    pass


class SpotUnavailable(BorroLiveError):
    """The MCP server does not list the spot tools (``buy_stock`` and ``sell_stock``), so ``buy`` and ``sell`` cannot run."""


class Unsafe(BorroLiveError):
    """The prepared transactions are not what was asked for (wrong chain, wrong wallet, value, or an approval too big)."""


@dataclass
class Result:
    """What one order did. ``sent`` is False for a dry run; ``ok`` is the simulation's verdict."""

    action: str
    symbol: str
    usdg: float | None
    live: bool
    sent: bool
    ok: bool
    tool: str
    quote: dict = field(default_factory=dict)
    simulation: dict = field(default_factory=dict)
    transactions: list = field(default_factory=list)
    note: str | None = None

    @property
    def hashes(self) -> list[str]:
        return [t["hash"] for t in self.transactions if t.get("hash")]

    def to_dict(self) -> dict:
        return asdict(self)


def _dec(x, places: int) -> str:
    """A decimal string in whole units, cut (never rounded up) to the token's places."""
    d = Decimal(str(x)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN)
    return format(d.normalize(), "f") if d else "0"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        v = float(raw)
    except ValueError:
        raise BorroLiveError(f"{name} must be a number of USDG") from None
    if not v > 0:
        raise BorroLiveError(f"{name} must be more than 0")
    return v


class Broker:
    """One wallet's desk on Borro.

    key             the agent's private key (default: the BORRO_AGENT_KEY environment variable). Never printed,
                    logged or stored. Without a key, give ``wallet`` (an address) for dry runs only.
    rpc_url         your own Robinhood Chain RPC (default: BORRO_RPC_URL). There is no built-in one.
    live            True to send. It also needs BORRO_LIVE=1 in the environment, or the broker refuses to start.
    max_order_usdg  the most one order may be worth (default 10, or BORRO_MAX_ORDER_USDG).
    max_day_usdg    the most all orders of one UTC day may be worth (default 25, or BORRO_MAX_DAY_USDG).
    builder_code    a Bookrunner code. When set, the wallet is bound to it once and lends go through BorroBuilders,
                    so the code's owner earns a share of Borro's fees. No default.
    slip_bps        the most a spot order's price may move against Borro's quote before it reverts, in hundredths of
                    a percent (default: Borro's own, 50).
    """

    def __init__(self, key: str | None = None, rpc_url: str | None = None, *, live: bool = False, wallet: str | None = None,
                 max_order_usdg: float | None = None, max_day_usdg: float | None = None, builder_code: str | None = None,
                 slip_bps: int | None = None, short_deposit: float = DEFAULT_SHORT_DEPOSIT,
                 mcp: McpClient | None = None, chain: Chain | None = None, state_path: str | os.PathLike | None = None):
        key = key or os.environ.get("BORRO_AGENT_KEY")
        self._account = account_from_key(key) if key else None
        wallet = wallet or os.environ.get("BORRO_WALLET") or None
        if self._account is None and not wallet:
            raise BorroLiveError("set BORRO_AGENT_KEY (the agent's own key), or for a dry run without a key set BORRO_WALLET (or give wallet=) to an address")
        self.wallet = self._account.address if self._account else wallet
        rpc_url = rpc_url or os.environ.get("BORRO_RPC_URL")
        self.chain = chain or (Chain(rpc_url) if rpc_url else None)
        if live:
            if os.environ.get("BORRO_LIVE") != "1":
                raise BorroLiveError("live=True also needs BORRO_LIVE=1 in the environment; without both, orders are dry runs")
            if self._account is None:
                raise BorroLiveError("live orders need the agent's key (BORRO_AGENT_KEY)")
            if self.chain is None:
                raise BorroLiveError("live orders need your own Robinhood Chain RPC (BORRO_RPC_URL)")
        self.live = bool(live)
        self.max_order_usdg = float(max_order_usdg) if max_order_usdg is not None else _env_float("BORRO_MAX_ORDER_USDG", DEFAULT_MAX_ORDER_USDG)
        self.max_day_usdg = float(max_day_usdg) if max_day_usdg is not None else _env_float("BORRO_MAX_DAY_USDG", DEFAULT_MAX_DAY_USDG)
        if not (self.max_order_usdg > 0 and self.max_day_usdg > 0):
            raise BorroLiveError("the caps must be more than 0")
        self.builder_code = (builder_code or "").strip().lower() or None
        if self.builder_code and not (self.builder_code.isascii() and len(self.builder_code) <= 31 and self.builder_code.replace("-", "").replace("_", "").isalnum()):
            raise BorroLiveError("a builder code is up to 31 letters, digits, - or _")
        if slip_bps is not None and not (isinstance(slip_bps, int) and not isinstance(slip_bps, bool) and 1 <= slip_bps <= 500):
            raise BorroLiveError("slip_bps is a whole number from 1 to 500 (50 is 0.5%)")
        if not short_deposit >= 0.5:
            raise BorroLiveError("short_deposit must be at least 0.5 (USDG deposited for each USDG shorted)")
        self.slip_bps = slip_bps
        self.short_deposit = short_deposit
        self.mcp = mcp or McpClient()
        self._state_path = Path(state_path or os.environ.get("BORRO_LIVE_STATE") or Path.home() / ".borro-live" / "state.json")
        self._markets: tuple[float, dict] | None = None
        self._spot_listed: bool | None = None

    def __repr__(self) -> str:
        return f"Broker(wallet={self.wallet}, live={self.live}, max_order_usdg={self.max_order_usdg}, max_day_usdg={self.max_day_usdg})"

    @classmethod
    def from_env(cls, **kw) -> "Broker":
        """BORRO_AGENT_KEY (or BORRO_WALLET, an address, for a dry run without a key), BORRO_RPC_URL and BORRO_LIVE; live when BORRO_LIVE=1 unless live= says otherwise."""
        kw.setdefault("live", os.environ.get("BORRO_LIVE") == "1")
        kw.setdefault("builder_code", os.environ.get("BORRO_BUILDER_CODE") or None)
        return cls(**kw)

    # ---- reads -------------------------------------------------------------------------------------------------

    def markets(self) -> dict:
        """Borro's shelf, kept for a minute."""
        if self._markets is None or time.time() - self._markets[0] > 60:
            self._markets = (time.time(), self.mcp.call("markets"))
        return self._markets[1]

    def tickers(self) -> list[str]:
        return [s["symbol"] for s in self.markets()["stocks"]]

    def _stock(self, symbol: str) -> dict:
        sym = str(symbol or "").strip().upper()
        for s in self.markets()["stocks"]:
            if s["symbol"] == sym:
                return s
        raise UnlistedTicker(f"{sym or 'that ticker'} is not on Borro; listed: {', '.join(self.tickers())}")

    def price(self, symbol: str) -> float:
        return float(self._stock(symbol)["price_usd"])

    def positions(self) -> dict:
        """What the wallet has, from the ``position`` tool: its balances, the stock tokens it holds, shorts and lends.

        ``holdings`` are the stock tokens in the wallet itself, which is what ``buy`` buys and ``sell`` sells.
        ``longs`` are leveraged longs on Borro (``buy`` opened one at 1.01x before 0.2.0; ``close_long`` closes it).
        """
        p = self.mcp.call("position", {"wallet": self.wallet})
        stocks = p.get("stocks") or []
        return {
            "wallet": p.get("wallet", self.wallet),
            "eth": (p.get("wallet_balances") or {}).get("eth"),
            "usdg": (p.get("wallet_balances") or {}).get("usdg"),
            "holdings": [{"symbol": s["symbol"], "shares": float(s["in_wallet"]), "value_usd": round(float(s["in_wallet"]) * float(s.get("price_usd") or 0), 6)}
                         for s in stocks if float(s.get("in_wallet") or 0) > 0],
            "longs": p.get("leveraged_longs") or [],
            "shorts": [{"symbol": s["symbol"], **s["short"]} for s in stocks if s.get("short")],
            "lent_usdg": (p.get("usdg_lent") or {}).get("usdg", 0),
            "stocks": stocks,
            "raw": p,
        }

    # ---- orders ------------------------------------------------------------------------------------------------

    def buy(self, symbol: str, usdg: float) -> Result:
        """Buy ``usdg`` worth of a stock token: a plain spot order (``buy_stock``).

        All the USDG is spent on Uniswap, through the stock's own pools, and the token arrives in this wallet. Nothing
        is lent, posted or borrowed. Borro's fee comes out of the USDG inside the swap; the quote shows it
        (``fee_usdg``) with the least stock the order accepts. ``sell`` swaps the token back to USDG.
        """
        s = self._stock(symbol)
        self._check_caps(usdg)
        self._need_spot()
        return self._order("buy", s["symbol"], usdg, "buy_stock", {"symbol": s["symbol"], "usdg": _dec(usdg, 6), **self._slip()}, spot_token=USDG)

    def sell(self, symbol: str, percent: float = 100, *, shares: float | None = None) -> Result:
        """Sell a stock token the wallet holds for USDG: a plain spot order (``sell_stock``).

        ``percent`` of what the wallet holds (default all of it), or exactly ``shares``. Stock lent on Borro is not in
        the wallet: take it back first. Borro's fee comes out of the USDG inside the swap.
        """
        s = self._stock(symbol)
        if shares is None and not (1 <= percent <= 100):
            raise BorroLiveError("percent must be from 1 to 100")
        if shares is not None and not float(shares) > 0:
            raise BorroLiveError("shares must be more than 0")
        self._need_spot()
        if shares is None and percent == 100:
            args = {"all": True}
        else:
            if shares is None:
                held = self.held(s["symbol"])
                if not held > 0:
                    raise BorroLiveError(f"the wallet holds no {s['symbol']} to sell")
                shares = Decimal(str(held)) * Decimal(str(percent)) / 100
            args = {"amount": _dec(shares, 18)}
        return self._order("sell", s["symbol"], None, "sell_stock", {"symbol": s["symbol"], **args, **self._slip()}, spot_token=s["contracts"]["token"])

    def close_long(self, symbol: str, percent: float = 100) -> Result:
        """Close a leveraged long on Borro (``close_long``): the stock is sold, the debt repaid, USDG comes back.

        ``buy`` no longer opens one. This is for a long opened by borro-live before 0.2.0 (a buy was a long at 1.01x
        then), or opened elsewhere; ``positions()["longs"]`` lists them.
        """
        s = self._stock(symbol)
        if not (1 <= percent <= 100):
            raise BorroLiveError("percent must be from 1 to 100")
        return self._order("close_long", s["symbol"], None, "close_long", {"symbol": s["symbol"], "percent": round(float(percent), 2)})

    def held(self, symbol: str) -> float:
        """How much of a stock token the wallet itself holds, in shares."""
        sym = str(symbol or "").strip().upper()
        return float(next((h["shares"] for h in self.positions()["holdings"] if h["symbol"] == sym), 0))

    def short(self, symbol: str, usdg: float) -> Result:
        """Short ``usdg`` worth of a stock: borrow it, sell it, hold the money plus a USDG deposit (``open_short``).

        A short can be liquidated: if the stock rises far enough it is closed for you at a loss. The deposit is
        ``short_deposit`` USDG per USDG shorted (default 1), never under the least Borro asks.
        """
        s = self._stock(symbol)
        self._check_caps(usdg)
        amount = _dec(Decimal(str(usdg)) / Decimal(str(s["price_usd"])), 18)
        deposit = float(usdg) * self.short_deposit
        args = {"symbol": s["symbol"], "amount": amount, "deposit": _dec(deposit, 2)}
        try:
            least = float(self.mcp.call("open_short", {"wallet": self.wallet, **args})["quote"]["deposit_at_least_usdg"])
        except ToolRefused:
            least = 0.0  # the order below gets the refusal and reports it
        if least * 1.1 > deposit:
            args["deposit"] = _dec(least * 1.1, 2)
        return self._order("short", s["symbol"], usdg, "open_short", args, usdg_out=float(args["deposit"]))

    def cover(self, symbol: str) -> Result:
        """Close the wallet's short on a stock, all of it (``close_short``): Borro has one short per stock."""
        s = self._stock(symbol)
        return self._order("cover", s["symbol"], None, "close_short", {"symbol": s["symbol"]})

    def lend(self, asset: str, amount: float) -> Result:
        """Lend USDG (it earns interest) or a stock token the wallet holds (it earns lending fees): ``supply``.

        ``amount`` is in the asset's own units: USDG, or shares of the stock. With a builder code, the deposit goes
        through BorroBuilders instead, so the code is credited.
        """
        a = str(asset or "").strip().upper()
        if a == "USDG":
            value, stock = float(amount), None
        else:
            stock = self._stock(a)
            value = float(amount) * float(stock["price_usd"])
        if self.builder_code:
            return self._lend_with_code(a, stock, amount, value)
        asked = _dec(amount, 6 if stock is None else 18)
        out = self._order("lend", a, value, "supply", {"asset": a, "amount": asked}, usdg_out=value if stock is None else 0)
        if Decimal(str(out.quote.get("amount", asked))) < Decimal(asked):
            out.note = f"Borro cut this lend to the {out.quote['amount']} {a} the wallet holds"
        return out

    def buy_spot_with_eth(self, symbol: str, shares: float) -> Result:
        """A plain spot buy, paid in the wallet's ETH (``buy_with_eth``): the stock token lands in the wallet.

        ``sell`` sells it for USDG and ``lend`` lends it, like a token ``buy`` bought.
        """
        s = self._stock(symbol)
        return self._order("buy_spot_with_eth", s["symbol"], float(shares) * float(s["price_usd"]), "buy_with_eth",
                           {"want": s["symbol"], "amount": _dec(shares, 18)}, pays_eth=True)

    # ---- the one path every order takes ------------------------------------------------------------------------

    def _order(self, action: str, symbol: str, usdg: float | None, tool: str, args: dict, *, usdg_out: float | None = None, pays_eth: bool = False,
               spot_token: str | None = None) -> Result:
        if usdg is not None:
            self._check_caps(usdg)
        note = self._bind() if usdg is not None and self.builder_code else None
        prep = self.mcp.call(tool, {"wallet": self.wallet, **args})
        if prep.get("live") is False:
            raise BorroLiveError(prep.get("note") or f"{tool} is not live on Borro")
        max_wei = int(Decimal(str((prep.get("quote") or {}).get("eth_at_most", "0"))).scaleb(18)) if pays_eth else 0
        self._check_prepared(prep, usdg if usdg_out is None else usdg_out, max_wei)
        if spot_token is not None:
            self._check_spot(prep, spot_token)
        return self._finish(action, symbol, usdg, tool, prep.get("quote") or {k: prep[k] for k in ("asset", "amount", "mode") if k in prep},
                            prep.get("simulation") or {}, prep["transactions"], note)

    def _finish(self, action, symbol, usdg, tool, quote, simulation, txs, note) -> Result:
        steps = simulation.get("steps") or []
        ok = simulation.get("ok") is True and len(steps) == len(txs) and all(s.get("ok") is True for s in steps)
        out = Result(action=action, symbol=symbol, usdg=usdg, live=self.live, sent=False, ok=ok, tool=tool, quote=quote, simulation=simulation, note=note,
                     transactions=[{"step": t.get("step", i + 1), "kind": t.get("kind"), "description": t.get("description"), "to": t["to"]} for i, t in enumerate(txs)])
        if not self.live:
            return out
        if not ok:
            why = "; ".join(f"step {s.get('step')}: {s.get('error', 'failed')}" for s in steps if s.get("ok") is not True) or simulation.get("error") or "no passing simulation"
            raise SimulationFailed(f"{action} {symbol}: the simulation failed, nothing was sent ({why})")
        if usdg is not None:
            self._spend(usdg)
        for row, t in zip(out.transactions, txs):
            row.update(self.chain.send(self._account, t))
        out.sent = True
        return out

    def _check_prepared(self, prep: dict, usdg_out: float | None, max_wei: int) -> None:
        txs = prep.get("transactions")
        if not isinstance(txs, list) or not txs:
            raise Unsafe("the tool returned no transactions")
        if prep.get("chainId") != CHAIN_ID or str(prep.get("from", "")).lower() != self.wallet.lower():
            raise Unsafe("the transactions are not for this wallet on Robinhood Chain")
        for t in txs:
            if t.get("chainId") != CHAIN_ID or not str(t.get("to", "")).startswith("0x") or not str(t.get("data", "")).startswith("0x"):
                raise Unsafe("a transaction is malformed or for another chain")
            if int(t.get("value", 0)) > max_wei:
                raise Unsafe("a transaction sends more ETH than this order's quote")
            # A USDG approval may never be for more than the order itself (a little over, for rounding): the token's
            # own approval, and Permit2's allowance of USDG to a spender.
            if usdg_out is not None and t["to"].lower() == USDG.lower() and t["data"].startswith(APPROVE):
                if int(t["data"][74:138], 16) > int(usdg_out * 1.02 * 1e6) + 10_000:
                    raise Unsafe("a USDG approval is for more than this order")
            if usdg_out is not None and t["to"].lower() == PERMIT2.lower() and t["data"].startswith(PERMIT2_APPROVE) and t["data"][34:74].lower() == USDG[2:].lower():
                if int(t["data"][138:202], 16) > int(usdg_out * 1.02 * 1e6) + 10_000:
                    raise Unsafe("a USDG approval is for more than this order")

    def _check_spot(self, prep: dict, token: str) -> None:
        """A spot order's steps: the token it spends approved to Permit2, Permit2's allowance of that token for the
        Universal Router, then the router's swap, in that order and nothing else."""
        txs = prep["transactions"]
        last = txs[-1]
        if last.get("kind") != "call" or last["to"].lower() != UNIVERSAL_ROUTER.lower():
            raise Unsafe("a spot order must end with a swap on Uniswap's Universal Router")
        for t in txs[:-1]:
            to, data = t["to"].lower(), t["data"]
            token_approval = to == token.lower() and data.startswith(APPROVE) and data[34:74].lower() == PERMIT2[2:].lower()
            permit_approval = (to == PERMIT2.lower() and data.startswith(PERMIT2_APPROVE) and data[34:74].lower() == token[2:].lower()
                               and data[98:138].lower() == UNIVERSAL_ROUTER[2:].lower())
            if t.get("kind") != "approve" or not (token_approval or permit_approval):
                raise Unsafe("a spot order may only approve the token it spends to Permit2, and Permit2 to Uniswap's Universal Router")

    def _slip(self) -> dict:
        return {} if self.slip_bps is None else {"slip_bps": self.slip_bps}

    def _need_spot(self) -> None:
        """Spot orders need Borro's ``buy_stock`` and ``sell_stock`` tools: asked once, refused in words without them."""
        if self._spot_listed is None:
            listed = {t.get("name") for t in self.mcp.tools_list()}
            self._spot_listed = all(name in listed for name in SPOT_TOOLS)
        if not self._spot_listed:
            raise SpotUnavailable(f"the MCP server at {self.mcp.url} does not list buy_stock and sell_stock, which this version's buy and sell need: "
                                  "nothing was sent. short, cover and lend work as before")

    # ---- caps --------------------------------------------------------------------------------------------------

    def _state(self) -> dict:
        try:
            return json.loads(self._state_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, state: dict) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1))
        tmp.replace(self._state_path)

    def spent_today(self) -> float:
        day = (self._state().get("days") or {}).get(self.wallet.lower()) or {}
        return float(day.get("usdg", 0)) if day.get("day") == time.strftime("%Y-%m-%d", time.gmtime()) else 0.0

    def _check_caps(self, usdg: float) -> None:
        usdg = float(usdg)
        if not usdg > 0:
            raise BorroLiveError("the order size must be more than 0 USDG")
        if usdg > self.max_order_usdg + 1e-9:
            raise CapExceeded(f"{usdg:g} USDG is over the per-order cap of {self.max_order_usdg:g} (max_order_usdg)")
        if self.spent_today() + usdg > self.max_day_usdg + 1e-9:
            raise CapExceeded(f"{usdg:g} USDG would pass today's cap of {self.max_day_usdg:g} ({self.spent_today():g} already sent; max_day_usdg, UTC day)")

    def _spend(self, usdg: float) -> None:
        """Counted before the first transaction goes out, so a crash mid-order never frees room under the cap."""
        state = self._state()
        state.setdefault("days", {})[self.wallet.lower()] = {"day": time.strftime("%Y-%m-%d", time.gmtime()), "usdg": round(self.spent_today() + float(usdg), 6)}
        self._save(state)

    # ---- builder code ------------------------------------------------------------------------------------------

    def _bind(self) -> str:
        """Bind the wallet to the builder code, once: one EIP-712 signature through ``bind_builder``, no transaction."""
        code, me = self.builder_code, self.wallet
        state = self._state()
        if (state.get("bound") or {}).get(me.lower()):
            return f"credited to builder code {state['bound'][me.lower()]}"
        first = self.mcp.call("bind_builder", {"wallet": me, "code": code})
        if not first.get("bound"):
            if not self.live:
                return f"a live order would first bind this wallet to builder code {code} (one signature, no gas)"
            td = first.get("typed_data") or {}
            msg, dom = td.get("message") or {}, td.get("domain") or {}
            # Sign only the one statement this is for, whatever the server sent.
            if not (td.get("primaryType") == "BuilderBinding" and dom == {"name": "Borro Builders", "version": "1", "chainId": CHAIN_ID}
                    and str(msg.get("account", "")).lower() == me.lower() and msg.get("code") == code and msg.get("chainId") == CHAIN_ID
                    and msg.get("statement") == f"Credit my Borro borrowing to builder code {code}" and set(msg) == {"statement", "account", "code", "chainId", "timestamp"}):
                raise Unsafe("the binding to sign is not the builder binding for this wallet and code")
            sig = self._account.sign_typed_data(full_message=td).signature.hex()
            first = self.mcp.call("bind_builder", {"wallet": me, "code": code, "timestamp": msg["timestamp"], "signature": sig if sig.startswith("0x") else "0x" + sig})
        bound = ((first.get("binding") or {}).get("code") or code) if first.get("bound") else None
        if not bound:
            raise BorroLiveError(f"the wallet could not be bound to builder code {code}")
        state = self._state()
        state.setdefault("bound", {})[me.lower()] = bound
        self._save(state)
        return f"credited to builder code {bound}" + ("" if bound == code else f" (the wallet was bound to it first; {code} is not credited)")

    def _lend_with_code(self, asset: str, stock: dict | None, amount: float, value: float) -> Result:
        """A lend through BorroBuilders.deposit(vault, assets, code, minShares): the wallet keeps the vault shares."""
        if self.chain is None:
            raise BorroLiveError("lending with a builder code reads the chain: set BORRO_RPC_URL")
        self._check_caps(value)
        token = USDG if stock is None else stock["contracts"]["token"]
        vault = self.markets()["usdg"]["vault"] if stock is None else stock["contracts"]["vault"]
        units = int(Decimal(_dec(amount, 6 if stock is None else 18)).scaleb(6 if stock is None else 18))
        code32 = self.builder_code.encode().ljust(32, b"\0")
        c, me = self.chain, self.wallet
        if int.from_bytes(c.call(BORRO_BUILDERS, calldata("builders(bytes32)", ["bytes32"], [code32]))[-20:], "big") == 0:
            raise BorroLiveError(f"no one has registered the builder code {self.builder_code}")
        if not c.read(BORRO_BUILDERS, "vaults(address)", ["address"], [vault], ["bool"])[0]:
            out = self._order("lend", asset, value, "supply", {"asset": asset, "amount": _dec(amount, 6 if stock is None else 18)}, usdg_out=value if stock is None else 0)
            out.note = f"BorroBuilders does not list the {asset} vault yet, so this lend went straight to the vault and is not credited to {self.builder_code}"
            return out
        if c.read(token, "balanceOf(address)", ["address"], [me], ["uint256"])[0] < units:
            raise BorroLiveError(f"the wallet holds less than {amount:g} {asset}")
        note = self._bind()
        txs = []
        if c.read(token, "allowance(address,address)", ["address", "address"], [me, BORRO_BUILDERS], ["uint256"])[0] < units:
            txs.append({"kind": "approve", "description": "Approve BorroBuilders to take exactly what this lends", "to": token,
                        "data": calldata("approve(address,uint256)", ["address", "uint256"], [BORRO_BUILDERS, units]), "value": "0", "chainId": CHAIN_ID})
        min_shares = c.read(vault, "previewDeposit(uint256)", ["uint256"], [units], ["uint256"])[0] * 995 // 1000
        txs.append({"kind": "call", "description": f"Lend {amount:g} {asset} through BorroBuilders with the code {self.builder_code}", "to": BORRO_BUILDERS,
                    "data": calldata("deposit(address,uint256,bytes32,uint256)", ["address", "uint256", "bytes32", "uint256"], [vault, units, code32, min_shares]), "value": "0", "chainId": CHAIN_ID})
        for i, t in enumerate(txs):
            t["step"] = i + 1
        steps = c.simulate(me, txs)
        sim = {"method": "eth_simulateV1", "ok": bool(steps) and all(s["ok"] for s in steps), "steps": steps or [],
               **({} if steps is not None else {"error": "this RPC does not offer eth_simulateV1, so the steps could not be simulated together"})}
        return self._finish("lend", asset, value, "BorroBuilders.deposit", {"asset": asset, "amount": _dec(amount, 6 if stock is None else 18), "builder_code": self.builder_code}, sim, txs, note)


__all__ = ["Broker", "Result", "BorroLiveError", "UnlistedTicker", "CapExceeded", "SimulationFailed", "SpotUnavailable", "Unsafe", "ChainError", "BORRO_BUILDERS", "USDG",
           "PERMIT2", "UNIVERSAL_ROUTER"]
