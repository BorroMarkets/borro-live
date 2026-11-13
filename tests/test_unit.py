"""Unit tests against recorded answers of Borro's MCP server. No network, no chain: python -m unittest discover tests"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eth_account import Account  # noqa: E402
from eth_account.messages import encode_typed_data  # noqa: E402
from eth_utils import keccak  # noqa: E402

from borro_live import Broker, CapExceeded, ChainError, McpClient, McpError, SimulationFailed, SpotUnavailable, ToolRefused, UnlistedTicker, Unsafe  # noqa: E402
from borro_live import ai_hedge_fund as ahf  # noqa: E402
from borro_live import tradingagents as ta  # noqa: E402
from borro_live.__main__ import main as cli  # noqa: E402
from borro_live.broker import BORRO_BUILDERS, PERMIT2, UNIVERSAL_ROUTER, USDG, BorroLiveError  # noqa: E402
from borro_live.chain import Chain  # noqa: E402

FIX = Path(__file__).parent / "fixtures"
KEY = "0x" + "5a" * 32  # a made-up key: the wallet the answers were recorded for
ME = Account.from_key(KEY).address
SECRET_RPC = "https://rpc.example/v2/SECRET-RPC-KEY"


def fx(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


class FakeMcp:
    """Replays recorded answers by tool name; ``answers`` overrides one, and every call is kept."""

    DEFAULT = {"markets": "markets", "position": "position_all", "buy_stock": "buy_stock", "sell_stock": "sell_stock", "close_long": "close_long",
               "open_short": "open_short", "close_short": "close_short", "supply": "supply_usdg", "buy_with_eth": "buy_with_eth", "bind_builder": "bind_typed"}

    def __init__(self, without_tools=(), **answers):
        self.answers = answers
        self.without_tools = set(without_tools)  # tools an older server does not list
        self.calls: list[tuple[str, dict]] = []
        self.lists = 0

    def __call__(self, payload: dict) -> dict:
        m = payload["method"]
        if m == "initialize":
            return fx("initialize")
        if m == "tools/list":
            self.lists += 1
            listed = fx("tools_list")
            listed["result"]["tools"] = [t for t in listed["result"]["tools"] if t["name"] not in self.without_tools]
            return listed
        name, args = payload["params"]["name"], payload["params"]["arguments"]
        self.calls.append((name, args))
        a = self.answers.get(name, self.DEFAULT.get(name))
        if callable(a):
            a = a(args)
        if a is None:
            return {"jsonrpc": "2.0", "id": payload["id"], "error": {"code": -32602, "message": "unknown tool"}}
        return fx(a) if isinstance(a, str) else a

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]


class FakeChain:
    """A JSON-RPC endpoint in memory: keeps what was sent, fails where a test tells it to."""

    def __init__(self, fail_receipt_at: int | None = None, revert_call_at: int | None = None, simulate=True, reads=None):
        self.sent: list[dict] = []
        self.eth_calls = 0
        self.fail_receipt_at, self.revert_call_at, self.simulate, self.reads = fail_receipt_at, revert_call_at, simulate, reads or {}

    def __call__(self, p: dict) -> dict:
        m, a = p["method"], p["params"]
        ok = lambda r: {"jsonrpc": "2.0", "id": p["id"], "result": r}  # noqa: E731
        if m == "eth_chainId":
            return ok(hex(4663))
        if m == "eth_call":
            if "from" in a[0]:
                self.eth_calls += 1
                if self.revert_call_at == self.eth_calls:
                    return {"jsonrpc": "2.0", "id": p["id"], "error": {"code": 3, "message": "execution reverted: insufficient collateral"}}
                return ok("0x")
            for sel, word in self.reads.items():
                if a[0]["data"].startswith(sel):
                    return ok("0x" + (word(a[0]) if callable(word) else word))
            return ok("0x" + "0" * 64)
        if m == "eth_estimateGas":
            return ok(hex(100_000))
        if m == "eth_getBlockByNumber":
            return ok({"baseFeePerGas": hex(10**8)})
        if m == "eth_maxPriorityFeePerGas":
            return ok("0x0")
        if m == "eth_getTransactionCount":
            return ok(hex(len(self.sent)))
        if m == "eth_sendRawTransaction":
            raw = bytes.fromhex(a[0][2:])
            self.sent.append({"raw": raw, "from": Account.recover_transaction(raw), "hash": "0x" + keccak(raw).hex()})
            return ok(self.sent[-1]["hash"])
        if m == "eth_getTransactionReceipt":
            n = next(i for i, s in enumerate(self.sent) if s["hash"] == a[0]) + 1
            return ok({"status": "0x0" if self.fail_receipt_at == n else "0x1", "gasUsed": hex(90_000), "blockNumber": hex(100 + n)})
        if m == "eth_simulateV1":
            if not self.simulate:
                return {"jsonrpc": "2.0", "id": p["id"], "error": {"code": -32601, "message": "method not found"}}
            return ok([{"calls": [{"status": "0x1"} for _ in a[0]["blockStateCalls"][0]["calls"]]}])
        raise AssertionError(m)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state.json"
        self._env = dict(os.environ)
        for k in list(os.environ):
            if k.startswith("BORRO_"):
                del os.environ[k]
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._env)))

    def broker(self, live=False, mcp=None, chain=None, **kw):
        self.mcp = mcp or FakeMcp()
        self.chain = chain or FakeChain()
        if live:
            os.environ["BORRO_LIVE"] = "1"
        return Broker(KEY, live=live, mcp=McpClient(transport=self.mcp), chain=Chain(SECRET_RPC, transport=self.chain), state_path=self.state, **kw)


class TestMcpClient(Base):
    def test_initialize_and_tools_list(self):
        c = McpClient(transport=FakeMcp())
        self.assertEqual(c.initialize()["serverInfo"]["name"], "borro")
        tools = {t["name"]: t for t in c.tools_list()}
        need = {"markets": [], "position": ["wallet"], "buy_stock": ["wallet", "symbol", "usdg"], "sell_stock": ["wallet", "symbol"], "close_long": ["wallet", "symbol"],
                "open_short": ["wallet", "symbol", "amount"], "close_short": ["wallet", "symbol"], "supply": ["wallet", "asset", "amount"],
                "bind_builder": ["wallet", "code"], "buy_with_eth": ["wallet", "want", "amount"]}
        for name, required in need.items():
            self.assertEqual(tools[name]["inputSchema"].get("required", []), required, name)

    def test_call_returns_the_structured_answer(self):
        self.assertEqual(len(McpClient(transport=FakeMcp()).call("markets")["stocks"]), 12)

    def test_a_refusal_in_words_raises(self):
        with self.assertRaisesRegex(ToolRefused, "no stock DOGE on Borro"):
            McpClient(transport=FakeMcp(supply="err_unlisted")).call("supply", {"asset": "DOGE"})

    def test_a_json_rpc_error_raises(self):
        with self.assertRaisesRegex(McpError, "unknown tool"):
            McpClient(transport=FakeMcp()).call("nothing")


class TestDryRun(Base):
    def test_dry_run_is_the_default_and_sends_nothing(self):
        b = self.broker()
        r = b.buy("nvda", 5)
        self.assertFalse(b.live)
        self.assertEqual((r.sent, r.ok, r.tool, r.symbol), (False, True, "buy_stock", "NVDA"))
        self.assertEqual([(t["kind"], t["to"]) for t in r.transactions], [("approve", USDG), ("approve", PERMIT2), ("call", UNIVERSAL_ROUTER)])
        self.assertEqual(r.simulation["method"], "eth_simulateV1")
        self.assertEqual(r.hashes, [])
        self.assertEqual(self.chain.sent, [])
        self.assertEqual(self.mcp.calls[-1], ("buy_stock", {"wallet": ME, "symbol": "NVDA", "usdg": "5"}))  # a plain spot order: no leverage, no loan
        self.assertEqual((r.quote["usdg_in"], r.quote["fee_usdg"], r.quote["fee_bps"]), ("5.0", "0.05", 100))  # Borro's fee is in the quote
        self.assertEqual(b.spent_today(), 0)

    def test_a_dry_sell_is_a_spot_sale_of_what_the_wallet_holds(self):
        r = self.broker().sell("NVDA")
        self.assertEqual((r.sent, r.ok, r.tool, r.action), (False, True, "sell_stock", "sell"))
        self.assertEqual(self.mcp.calls[-1], ("sell_stock", {"wallet": ME, "symbol": "NVDA", "all": True}))
        token = fx("markets")["result"]["structuredContent"]["stocks"][2]["contracts"]["token"]
        self.assertEqual([(t["kind"], t["to"]) for t in r.transactions], [("approve", token), ("approve", PERMIT2), ("call", UNIVERSAL_ROUTER)])
        self.assertEqual(self.chain.sent, [])

    def test_a_dry_run_needs_no_key_and_no_rpc(self):
        b = Broker(wallet=ME, mcp=McpClient(transport=FakeMcp()), state_path=self.state)
        self.assertTrue(b.buy("NVDA", 5).ok)
        self.assertIsNone(b.chain)

    def test_a_failing_simulation_is_reported_not_raised(self):
        r = self.broker(mcp=FakeMcp(open_short="open_short_nodep")).short("NVDA", 5)
        self.assertEqual((r.ok, r.sent), (False, False))
        self.assertEqual(r.simulation["steps"][0]["error"], "insufficient collateral")

    def test_live_needs_the_flag_and_the_environment(self):
        with self.assertRaisesRegex(BorroLiveError, "BORRO_LIVE=1"):
            Broker(KEY, SECRET_RPC, live=True, state_path=self.state)
        os.environ["BORRO_LIVE"] = "1"
        self.assertFalse(Broker(KEY, SECRET_RPC, state_path=self.state).live)  # the environment alone is not enough
        self.assertTrue(Broker(KEY, SECRET_RPC, live=True, state_path=self.state).live)
        with self.assertRaisesRegex(BorroLiveError, "RPC"):
            Broker(KEY, live=True, state_path=self.state)
        with self.assertRaisesRegex(BorroLiveError, "key"):
            Broker(wallet=ME, rpc_url=SECRET_RPC, live=True, state_path=self.state)

    def test_from_env(self):
        os.environ.update(BORRO_AGENT_KEY=KEY, BORRO_RPC_URL=SECRET_RPC, BORRO_MAX_ORDER_USDG="3", BORRO_MAX_DAY_USDG="7")
        b = Broker.from_env(state_path=self.state)
        self.assertEqual((b.live, b.wallet, b.max_order_usdg, b.max_day_usdg, b.builder_code), (False, ME, 3.0, 7.0, None))
        os.environ["BORRO_LIVE"] = "1"
        self.assertTrue(Broker.from_env(state_path=self.state).live)

    def test_no_key_and_no_wallet_is_refused(self):
        with self.assertRaisesRegex(BorroLiveError, "BORRO_AGENT_KEY"):
            Broker(state_path=self.state)


class TestSafety(Base):
    def test_an_unlisted_ticker_is_refused_before_any_trade_tool(self):
        b = self.broker(live=True)
        for order in (lambda: b.buy("DOGE", 5), lambda: b.short("BTC", 5), lambda: b.sell("XYZ"), lambda: b.cover(""), lambda: b.lend("ETH", 1)):
            with self.assertRaisesRegex(UnlistedTicker, "not on Borro; listed: SPY"):
                order()
        self.assertEqual(set(self.mcp.names()), {"markets"})
        self.assertEqual(self.chain.sent, [])

    def test_the_per_order_cap(self):
        b = self.broker(live=True)
        self.assertEqual(b.max_order_usdg, 10)
        with self.assertRaisesRegex(CapExceeded, "per-order cap of 10"):
            b.buy("NVDA", 10.01)
        with self.assertRaisesRegex(CapExceeded, "per-order cap"):
            b.short("NVDA", 50)
        with self.assertRaisesRegex(CapExceeded, "per-order cap"):
            b.lend("USDG", 11)
        with self.assertRaisesRegex(BorroLiveError, "more than 0"):
            b.buy("NVDA", 0)
        self.assertEqual(self.chain.sent, [])

    def test_the_per_day_cap_counts_live_orders_and_survives_a_restart(self):
        b = self.broker(live=True, max_order_usdg=5, max_day_usdg=12)
        b.buy("NVDA", 5)
        b.buy("NVDA", 5)
        self.assertEqual(b.spent_today(), 10)
        again = self.broker(live=True, max_order_usdg=5, max_day_usdg=12)  # a new process reads the same file
        with self.assertRaisesRegex(CapExceeded, r"today's cap of 12 \(10 already sent"):
            again.buy("NVDA", 5)
        self.assertEqual(self.chain.sent, [])
        self.assertNotIn(KEY[2:], self.state.read_text())

    def test_closing_is_never_stopped_by_a_cap(self):
        b = self.broker(live=True, max_order_usdg=1, max_day_usdg=1)
        self.assertTrue(b.sell("NVDA").sent)
        self.assertTrue(b.cover("NVDA").sent)
        self.assertEqual(b.spent_today(), 0)

    def test_a_failed_simulation_is_never_sent(self):
        b = self.broker(live=True, mcp=FakeMcp(open_short="open_short_nodep"))
        with self.assertRaisesRegex(SimulationFailed, "insufficient collateral"):
            b.short("NVDA", 5)
        self.assertEqual(self.chain.sent, [])
        self.assertEqual(b.spent_today(), 0)

    def test_a_simulation_that_does_not_cover_every_step_is_never_sent(self):
        prep = fx("buy_stock")
        prep["result"]["structuredContent"]["simulation"]["steps"].pop()
        with self.assertRaises(SimulationFailed):
            self.broker(live=True, mcp=FakeMcp(buy_stock=prep)).buy("NVDA", 5)
        self.assertEqual(self.chain.sent, [])

    def test_transactions_for_another_wallet_chain_or_with_eth_are_refused(self):
        def bent(change):
            prep = fx("buy_stock")
            change(prep["result"]["structuredContent"])
            return prep
        cases = [lambda s: s.update({"from": "0x" + "11" * 20}), lambda s: s.update(chainId=1), lambda s: s["transactions"][2].update(chainId=1),
                 lambda s: s["transactions"][2].update(value="1"), lambda s: s.update(transactions=[])]
        for change in cases:
            b = self.broker(live=True, mcp=FakeMcp(buy_stock=bent(change)))
            with self.assertRaises(Unsafe):
                b.buy("NVDA", 5)
            self.assertEqual(self.chain.sent, [])

    def test_a_usdg_approval_over_the_order_is_refused(self):
        prep = fx("buy_stock")
        t = prep["result"]["structuredContent"]["transactions"][0]
        self.assertEqual(t["to"], USDG)
        t["data"] = t["data"][:74] + "f" * 64 + t["data"][138:]
        with self.assertRaisesRegex(Unsafe, "approval"):
            self.broker(live=True, mcp=FakeMcp(buy_stock=prep)).buy("NVDA", 5)
        self.assertEqual(self.chain.sent, [])
        prep = fx("buy_stock")  # Permit2's allowance of USDG for the router, for more than the order
        t = prep["result"]["structuredContent"]["transactions"][1]
        self.assertEqual(t["to"], PERMIT2)
        t["data"] = t["data"][:138] + "0" * 24 + "f" * 40 + t["data"][202:]
        with self.assertRaisesRegex(Unsafe, "approval"):
            self.broker(live=True, mcp=FakeMcp(buy_stock=prep)).buy("NVDA", 5)
        self.assertEqual(self.chain.sent, [])

    def test_a_spot_order_may_only_approve_its_token_to_permit2_and_swap_on_the_router(self):
        other = "11" * 20

        def bent(tool, change):
            prep = fx(tool)
            change(prep["result"]["structuredContent"]["transactions"])
            return prep
        cases = [
            lambda t: t[0].update(data=t[0]["data"][:34] + other + t[0]["data"][74:]),      # the token approved to someone else
            lambda t: t[1].update(data=t[1]["data"][:98] + other + t[1]["data"][138:]),     # Permit2 told to let someone else take it
            lambda t: t[1].update(data=t[1]["data"][:34] + other + t[1]["data"][74:]),      # Permit2's allowance for another token
            lambda t: t[2].update(to="0x" + other),                                         # the swap sent somewhere else
            lambda t: t[2].update(kind="approve"),
            lambda t: t.insert(0, {**t[0], "to": "0x" + other}),                            # an approval on a token this order does not spend
            lambda t: t.append(dict(t[0])),                                                 # an approval after the swap
        ]
        for tool, order in (("buy_stock", lambda b: b.buy("NVDA", 5)), ("sell_stock", lambda b: b.sell("NVDA"))):
            for i, change in enumerate(cases):
                b = self.broker(live=True, mcp=FakeMcp(**{tool: bent(tool, change)}))
                with self.assertRaises(Unsafe, msg=f"{tool} case {i}"):
                    order(b)
                self.assertEqual(self.chain.sent, [])

    def test_buy_and_sell_need_the_spot_tools_and_say_so_when_the_server_has_none(self):
        b = self.broker(live=True, mcp=FakeMcp(without_tools=("buy_stock", "sell_stock")))
        for order in (lambda: b.buy("NVDA", 5), lambda: b.sell("NVDA"), lambda: b.sell("NVDA", shares=0.01)):
            with self.assertRaisesRegex(SpotUnavailable, "does not list buy_stock and sell_stock.*nothing was sent"):
                order()
        self.assertEqual((set(self.mcp.names()), self.mcp.lists, self.chain.sent, b.spent_today()), ({"markets"}, 1, [], 0))  # asked once, nothing built
        self.assertTrue(issubclass(SpotUnavailable, BorroLiveError))
        self.assertTrue(b.short("NVDA", 5).sent)  # the other orders do not need them
        with self.assertRaises(SpotUnavailable):
            ahf.BorroBroker(self.broker(live=True, mcp=FakeMcp(without_tools=("buy_stock",)), max_order_usdg=1000, max_day_usdg=1000)).place_order(order_of("NVDA", "buy", 1))
        ok = self.broker()
        ok.buy("NVDA", 5), ok.sell("NVDA"), ok.buy("NVDA", 5)
        self.assertEqual(self.mcp.lists, 1)  # a server that lists them is asked once

    def test_spot_orders_that_are_not_live_on_borro_are_refused_in_borros_words(self):
        off = {"jsonrpc": "2.0", "id": 1, "result": {"content": [], "structuredContent": {"live": False, "note": "Spot buys and sells are not live on Borro yet."}}}
        b = self.broker(live=True, mcp=FakeMcp(buy_stock=off, sell_stock=off))
        for order in (lambda: b.buy("NVDA", 5), lambda: b.sell("NVDA")):
            with self.assertRaisesRegex(BorroLiveError, "not live on Borro yet"):
                order()
        self.assertEqual((self.chain.sent, b.spent_today()), ([], 0))

    def test_slip_bps_is_passed_on_and_checked(self):
        b = self.broker(slip_bps=100)
        b.buy("NVDA", 5)
        self.assertEqual(self.mcp.calls[-1][1], {"wallet": ME, "symbol": "NVDA", "usdg": "5", "slip_bps": 100})
        b.sell("NVDA")
        self.assertEqual(self.mcp.calls[-1][1], {"wallet": ME, "symbol": "NVDA", "all": True, "slip_bps": 100})
        for bad in (0, 501, 1.5, True):
            with self.assertRaisesRegex(BorroLiveError, "slip_bps"):
                self.broker(slip_bps=bad)

    def test_the_key_and_the_rpc_address_are_never_shown(self):
        b = self.broker(live=True)
        r = b.buy("NVDA", 5)
        shown = repr(b) + repr(b.chain) + json.dumps(r.to_dict()) + self.state.read_text() + json.dumps(vars(b), default=repr)
        for secret in (KEY, KEY[2:], "SECRET-RPC-KEY"):
            self.assertNotIn(secret, shown)

        def down(payload):
            raise McpError("the RPC could not be reached")
        with self.assertRaises(ChainError) as e:
            Chain(SECRET_RPC, transport=down).rpc("eth_chainId")
        self.assertNotIn("SECRET", str(e.exception))
        with self.assertRaises(ChainError) as e:
            Chain("http://127.0.0.1:9/SECRET-RPC-KEY", timeout=2).rpc("eth_chainId")  # a real refused connection
        self.assertNotIn("SECRET", str(e.exception) + repr(e.exception))
        with self.assertRaises(ValueError) as e:
            Broker("0xnot-a-key-" + "ab" * 8, state_path=self.state)
        self.assertNotIn("ab" * 8, str(e.exception))


class TestLive(Base):
    def test_a_live_buy_signs_and_sends_each_step_in_order(self):
        b = self.broker(live=True)
        r = b.buy("NVDA", 5)
        prep = fx("buy_stock")["result"]["structuredContent"]["transactions"]
        self.assertTrue(r.sent and r.ok and r.live)
        self.assertEqual(len(self.chain.sent), 3)
        self.assertEqual(r.hashes, [s["hash"] for s in self.chain.sent])
        for i, (sent, want) in enumerate(zip(self.chain.sent, prep)):
            self.assertEqual(sent["from"], ME)
            self.assertIn(bytes.fromhex(want["data"][2:]), sent["raw"])  # the calldata exactly as Borro built it, agent tag and all
            self.assertIn(bytes.fromhex(want["to"][2:]), sent["raw"])
            self.assertEqual(r.transactions[i]["status"], 1)
        self.assertEqual(self.chain.eth_calls, 3)  # each step run from the wallet before it is sent
        self.assertEqual(b.spent_today(), 5)

    def test_a_step_that_would_revert_is_not_sent(self):
        b = self.broker(live=True, chain=FakeChain(revert_call_at=2))
        with self.assertRaisesRegex(ChainError, "insufficient collateral"):
            b.buy("NVDA", 5)
        self.assertEqual(len(self.chain.sent), 1)  # the approval landed; the step that would fail and the one after never went

    def test_a_failed_receipt_stops_the_order(self):
        b = self.broker(live=True, chain=FakeChain(fail_receipt_at=1))
        with self.assertRaisesRegex(ChainError, "mined and failed"):
            b.buy("NVDA", 5)
        self.assertEqual(len(self.chain.sent), 1)

    def test_the_wrong_chain_is_refused(self):
        chain = FakeChain()
        b = self.broker(live=True, chain=lambda p: {"jsonrpc": "2.0", "id": 1, "result": "0x1"} if p["method"] == "eth_chainId" else chain(p))
        with self.assertRaisesRegex(ChainError, "not Robinhood Chain"):
            b.buy("NVDA", 5)
        self.assertEqual(chain.sent, [])

    def test_short_deposits_the_notional_and_never_under_borros_least(self):
        b = self.broker(live=True)
        r = b.short("NVDA", 5)
        price = b.price("NVDA")
        args = self.mcp.calls[-1][1]
        self.assertEqual(self.mcp.names()[-2:], ["open_short", "open_short"])
        self.assertAlmostEqual(float(args["amount"]) * price, 5, places=6)
        self.assertEqual(args["deposit"], "5")  # 1 USDG for each USDG shorted, over the 3.1 Borro asks at least
        self.assertTrue(r.sent)
        thin = self.broker(live=False, short_deposit=0.5)
        thin.short("NVDA", 5)
        self.assertEqual(self.mcp.calls[-1][1]["deposit"], "3.41")  # 0.5 would be 2.5: raised to the least and a tenth

    def test_sell_cover_and_lend_map_to_their_tools(self):
        b = self.broker(live=True)
        held = fx("position_all")["result"]["structuredContent"]["stocks"][0]["in_wallet"]
        self.assertEqual(b.held("nvda"), held)
        self.assertEqual(b.sell("NVDA", 50).tool, "sell_stock")  # half of what the wallet holds, in shares
        self.assertEqual(self.mcp.calls[-1][0], "sell_stock")
        self.assertEqual(set(self.mcp.calls[-1][1]), {"wallet", "symbol", "amount"})
        self.assertAlmostEqual(float(self.mcp.calls[-1][1]["amount"]), held / 2, places=15)
        b.sell("NVDA", shares=0.01)
        self.assertEqual(self.mcp.calls[-1], ("sell_stock", {"wallet": ME, "symbol": "NVDA", "amount": "0.01"}))
        r = b.sell("NVDA")
        self.assertEqual(self.mcp.calls[-1], ("sell_stock", {"wallet": ME, "symbol": "NVDA", "all": True}))
        self.assertEqual((r.sent, len(r.hashes), r.usdg), (True, 3, None))
        self.assertEqual(b.close_long("NVDA", 50).tool, "close_long")  # a leveraged long, as buy opened before 0.2.0
        self.assertEqual(self.mcp.calls[-1], ("close_long", {"wallet": ME, "symbol": "NVDA", "percent": 50.0}))
        self.assertEqual(b.cover("NVDA").tool, "close_short")
        self.assertEqual(self.mcp.calls[-1], ("close_short", {"wallet": ME, "symbol": "NVDA"}))
        r = b.lend("USDG", 5)
        self.assertEqual((r.tool, r.sent, len(r.hashes)), ("supply", True, 2))
        self.assertEqual(self.mcp.calls[-1], ("supply", {"wallet": ME, "asset": "USDG", "amount": "5"}))
        for bad in (lambda: b.sell("NVDA", 0), lambda: b.sell("NVDA", 101), lambda: b.close_long("NVDA", 0)):
            with self.assertRaisesRegex(BorroLiveError, "percent"):
                bad()
        with self.assertRaisesRegex(BorroLiveError, "shares must be more than 0"):
            b.sell("NVDA", shares=0)

    def test_a_part_sale_of_a_stock_the_wallet_does_not_hold_is_refused_before_borro_is_asked(self):
        b = self.broker(live=True)
        with self.assertRaisesRegex(BorroLiveError, "the wallet holds no TSLA to sell"):
            b.sell("TSLA", 50)
        self.assertNotIn("sell_stock", self.mcp.names())
        self.assertEqual(self.chain.sent, [])

    def test_a_lend_borro_cut_to_the_balance_says_so(self):
        prep = fx("supply_usdg")
        prep["result"]["structuredContent"]["amount"] = "0.5"
        self.assertIn("cut this lend to the 0.5 USDG", self.broker(mcp=FakeMcp(supply=prep)).lend("USDG", 5).note)

    def test_spot_buy_with_eth_may_send_eth_up_to_its_quote(self):
        b = self.broker(live=True)
        r = b.buy_spot_with_eth("NVDA", 0.02)
        self.assertEqual((r.tool, r.sent), ("buy_with_eth", True))
        prep = fx("buy_with_eth")
        t = prep["result"]["structuredContent"]["transactions"][0]
        t["value"] = str(int(t["value"]) * 2)
        with self.assertRaisesRegex(Unsafe, "more ETH"):
            self.broker(live=True, mcp=FakeMcp(buy_with_eth=prep)).buy_spot_with_eth("NVDA", 0.02)

    def test_positions(self):
        p = self.broker().positions()
        self.assertEqual(p["wallet"], ME)
        self.assertEqual([(x["symbol"], x["shares"]) for x in p["holdings"]], [("NVDA", 0.030666928810419228)])  # the stock token in the wallet itself
        self.assertAlmostEqual(p["holdings"][0]["value_usd"], 0.030666928810419228 * 160.68195761, places=6)
        self.assertEqual([x["symbol"] for x in p["longs"]], ["NVDA"])  # a leveraged long is listed apart: buy does not open one
        self.assertEqual([(x["symbol"], x["owed_stock"] > 0) for x in p["shorts"]], [("NVDA", True)])
        self.assertAlmostEqual(p["lent_usdg"], 5, places=3)
        self.assertGreater(p["usdg"], 0)


class TestBuilderCode(Base):
    READS = {"0x" + keccak(text="builders(bytes32)")[:4].hex(): "0" * 24 + "ab" * 20,
             "0x" + keccak(text="vaults(address)")[:4].hex(): "0" * 63 + "1",
             "0x" + keccak(text="balanceOf(address)")[:4].hex(): hex(10**12)[2:].rjust(64, "0"),
             "0x" + keccak(text="previewDeposit(uint256)")[:4].hex(): hex(4_900_000)[2:].rjust(64, "0")}

    def bind_answers(self, seen):
        def bind(args):
            seen.append(args)
            if "signature" in args:
                return {"jsonrpc": "2.0", "id": 1, "result": {"content": [], "structuredContent": {"action": "bind_builder", "bound": True, "binding": {"account": ME, "code": args["code"], "timestamp": args["timestamp"]}}}}
            return fx("bind_typed")
        return bind

    def test_there_is_no_default_code_and_none_is_bound_without_one(self):
        b = self.broker(live=True)
        self.assertIsNone(b.builder_code)
        b.buy("NVDA", 5)
        self.assertNotIn("bind_builder", self.mcp.names())

    def test_the_wallet_is_bound_once_with_its_own_signature(self):
        seen = []
        b = self.broker(live=True, mcp=FakeMcp(bind_builder=self.bind_answers(seen)), builder_code="trustware")
        r = b.buy("NVDA", 5)
        self.assertEqual(r.note, "credited to builder code trustware")
        self.assertEqual(len(seen), 2)
        typed = fx("bind_typed")["result"]["structuredContent"]["typed_data"]
        self.assertEqual(seen[1]["timestamp"], typed["message"]["timestamp"])
        self.assertEqual(Account.recover_message(encode_typed_data(full_message=typed), signature=seen[1]["signature"]), ME)
        b.buy("NVDA", 5)
        self.broker(live=True, mcp=FakeMcp(bind_builder=self.bind_answers(seen)), builder_code="trustware").short("NVDA", 5)
        self.assertEqual(len(seen), 2)  # once: kept in the state file, not asked again

    def test_a_dry_run_signs_nothing(self):
        seen = []
        r = self.broker(mcp=FakeMcp(bind_builder=self.bind_answers(seen)), builder_code="trustware").buy("NVDA", 5)
        self.assertEqual(len(seen), 1)
        self.assertIn("would first bind", r.note)

    def test_only_the_builder_binding_is_ever_signed(self):
        for change in (lambda t: t["message"].update(statement="Send all my USDG to someone"), lambda t: t["message"].update(code="other"),
                       lambda t: t["message"].update(account="0x" + "11" * 20), lambda t: t["domain"].update(name="Permit2"),
                       lambda t: t.update(primaryType="Permit"), lambda t: t["domain"].update(verifyingContract=USDG)):
            typed = fx("bind_typed")
            change(typed["result"]["structuredContent"]["typed_data"])
            seen = []
            b = self.broker(live=True, mcp=FakeMcp(bind_builder=lambda a, typed=typed, seen=seen: (seen.append(a), typed)[1]), builder_code="trustware")
            with self.assertRaisesRegex(Unsafe, "not the builder binding"):
                b.buy("NVDA", 5)
            self.assertEqual(len(seen), 1)
            self.assertEqual(self.chain.sent, [])

    def test_a_wallet_bound_to_another_code_is_told(self):
        other = {"jsonrpc": "2.0", "id": 1, "result": {"content": [], "structuredContent": {"action": "bind_builder", "bound": True, "binding": {"account": ME, "code": "route"}}}}
        r = self.broker(live=True, mcp=FakeMcp(bind_builder=other), builder_code="trustware").buy("NVDA", 5)
        self.assertIn("builder code route", r.note)
        self.assertIn("trustware is not credited", r.note)

    def test_a_lend_with_a_code_goes_through_borro_builders(self):
        seen = []
        b = self.broker(live=True, mcp=FakeMcp(bind_builder=self.bind_answers(seen)), chain=FakeChain(reads=self.READS), builder_code="trustware")
        r = b.lend("USDG", 5)
        self.assertEqual((r.tool, r.sent, r.ok), ("BorroBuilders.deposit", True, True))
        self.assertNotIn("supply", self.mcp.names())
        self.assertEqual([t["to"] for t in r.transactions], [USDG, BORRO_BUILDERS])
        approve, deposit = (s["raw"].hex() for s in self.chain.sent)
        vault = fx("markets")["result"]["structuredContent"]["usdg"]["vault"][2:].lower()
        self.assertIn("095ea7b3" + BORRO_BUILDERS[2:].lower().rjust(64, "0") + hex(5_000_000)[2:].rjust(64, "0"), approve)
        want = keccak(text="deposit(address,uint256,bytes32,uint256)")[:4].hex() + vault.rjust(64, "0") + hex(5_000_000)[2:].rjust(64, "0") \
            + b"trustware".ljust(32, b"\0").hex() + hex(4_900_000 * 995 // 1000)[2:].rjust(64, "0")
        self.assertIn(want, deposit)
        self.assertEqual(b.spent_today(), 5)

    def test_an_unregistered_code_or_an_rpc_without_simulation_sends_nothing(self):
        reads = dict(self.READS)
        reads["0x" + keccak(text="builders(bytes32)")[:4].hex()] = "0" * 64
        b = self.broker(live=True, chain=FakeChain(reads=reads), builder_code="nobody")
        with self.assertRaisesRegex(BorroLiveError, "no one has registered"):
            b.lend("USDG", 5)
        seen = []
        b = self.broker(live=True, mcp=FakeMcp(bind_builder=self.bind_answers(seen)), chain=FakeChain(reads=self.READS, simulate=False), builder_code="trustware")
        with self.assertRaisesRegex(SimulationFailed, "eth_simulateV1"):
            b.lend("USDG", 5)
        self.assertEqual(self.chain.sent, [])

    def test_a_bad_code_is_refused(self):
        with self.assertRaises(BorroLiveError):
            self.broker(builder_code="not a code!")


class Graph:
    """What a TradingAgentsGraph looks like from outside: propagate returns (final_state, signal)."""

    def __init__(self, signal):
        self.signal = signal

    def propagate(self, company_name, trade_date, asset_type="stock", portfolio=None):
        return {"company_of_interest": company_name, "final_trade_decision": f"Rating: {self.signal}"}, self.signal


class TestTradingAgents(Base):
    FLAT = {"position": lambda a: {"jsonrpc": "2.0", "id": 1, "result": {"content": [], "structuredContent": {"wallet": ME, "wallet_balances": {"eth": 1, "usdg": 100}, "stocks": [], "leveraged_longs": []}}}}

    def test_rating_of(self):
        for raw, want in [("Buy", "buy"), ("BUY", "buy"), ("**SELL**", "sell"), ("Overweight", "overweight"), ("underweight", "underweight"), (" hold\n", "hold"),
                          ("REVIEW", None), ("", None), (None, None), ("Buy the dip, then sell", None), (({"x": 1}, "Sell"), "sell"), ({"final_rating": "Hold"}, "hold")]:
            self.assertEqual(ta.rating_of(raw), want, raw)

    def test_each_rating_with_no_position(self):
        # the recorded buy_stock approves 5 USDG, so Overweight (half the size) is asked with 10
        for rating, size, want in [("Buy", 5, [("buy_stock", "5")]), ("Overweight", 10, [("buy_stock", "5")]), ("Hold", 5, []), ("Underweight", 5, []), ("Sell", 5, []), ("REVIEW", 5, [])]:
            b = self.broker(mcp=FakeMcp(**self.FLAT))
            out = ta.execute(b, "NVDA", rating, usdg=size)
            self.assertEqual([(r.tool, self.mcp.calls[-1][1].get("usdg")) for r in out], want, rating)

    def test_each_rating_with_the_stock_held_and_a_short_open(self):
        b = self.broker()
        held = b.held("NVDA")
        self.assertEqual([r.tool for r in ta.execute(b, "NVDA", "Buy")], ["close_short", "buy_stock"])
        self.assertEqual([r.tool for r in ta.execute(b, "NVDA", "Underweight")], ["sell_stock"])
        self.assertAlmostEqual(float(self.mcp.calls[-1][1]["amount"]), held / 2, places=15)  # half of what the wallet holds
        ta.execute(b, "NVDA", "SELL")
        self.assertEqual(self.mcp.calls[-1], ("sell_stock", {"wallet": ME, "symbol": "NVDA", "all": True}))

    def test_a_leveraged_long_alone_is_not_something_to_sell(self):
        # a wallet with a long opened before 0.2.0 and no stock in the wallet: Sell has nothing to sell
        def flat(args):
            a = fx("position_all")
            del a["result"]["structuredContent"]["stocks"][0]["in_wallet"]
            return a
        b = self.broker(mcp=FakeMcp(position=flat))
        self.assertEqual([p["symbol"] for p in b.positions()["longs"]], ["NVDA"])
        self.assertEqual(ta.execute(b, "NVDA", "Sell"), [])
        self.assertNotIn("sell_stock", self.mcp.names())

    def test_sell_shorts_only_when_asked(self):
        b = self.broker(mcp=FakeMcp(**self.FLAT))
        self.assertEqual(ta.execute(b, "NVDA", "Sell"), [])
        self.assertEqual([r.tool for r in ta.execute(b, "NVDA", "Sell", allow_short=True)], ["open_short"])

    def test_go_live_wraps_propagate_and_changes_nothing_it_returns(self):
        g = ta.go_live(Graph("Buy"), self.broker(mcp=FakeMcp(**self.FLAT)), usdg=5)
        state, signal = g.propagate("NVDA", "2026-09-01", asset_type="stock")
        self.assertEqual((state["company_of_interest"], signal), ("NVDA", "Buy"))
        self.assertEqual([(r.action, r.usdg, r.sent) for r in g.borro_orders], [("buy", 5, False)])
        self.assertIsNone(g.borro_error)

    def test_go_live_keeps_the_decision_when_the_order_is_refused(self):
        g = ta.go_live(Graph("Buy"), self.broker(mcp=FakeMcp(**self.FLAT)), usdg=4)
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            self.assertEqual(g.propagate("BTC-USD", "2026-09-01")[1], "Buy")
        self.assertIsInstance(g.borro_error, UnlistedTicker)
        self.assertEqual((g.borro_orders, len(w)), ([], 1))
        g = ta.go_live(Graph("REVIEW"), self.broker(), usdg=4)
        g.propagate("NVDA", "2026-09-01")
        self.assertEqual((g.borro_orders, self.mcp.names()), ([], []))


class TestAiHedgeFund(Base):
    DECISIONS = {"NVDA": {"action": "buy", "quantity": 3, "confidence": 80, "reasoning": "x"}, "TSLA": {"action": "short", "quantity": 1},
                 "SPY": {"action": "hold", "quantity": 0}, "DOGE": {"action": "buy", "quantity": 1}, "QQQ": {"action": "sell", "quantity": 1}}

    def test_decisions_map_to_orders_and_one_refusal_stops_nothing(self):
        b = self.broker(max_order_usdg=10)
        out = ahf.execute_decisions(b, {"decisions": self.DECISIONS, "analyst_signals": {}}, scale=0.01)
        self.assertEqual(out["NVDA"].tool, "buy_stock")
        self.assertAlmostEqual(out["NVDA"].usdg, 0.03 * b.price("NVDA"), places=6)
        self.assertEqual(out["TSLA"].tool, "open_short")
        self.assertIsNone(out["SPY"])
        self.assertIn("refused: DOGE is not on Borro", out["DOGE"])
        self.assertIn("refused: the wallet holds no QQQ to sell", out["QQQ"])

    def test_whole_shares_meet_the_cap(self):
        out = ahf.execute_decisions(self.broker(), {"NVDA": {"action": "buy", "quantity": 3}})
        self.assertIn("refused", out["NVDA"])
        self.assertIn("per-order cap", out["NVDA"])

    def test_sell_is_shares_the_wallet_holds_and_cover_closes_the_short(self):
        b = self.broker()
        held = ahf._held_shares(b.positions(), "NVDA")
        self.assertGreater(held, 0)

        class D:  # a decision object, as the portfolio manager's pydantic model
            def __init__(self, action, quantity):
                self.action, self.quantity = action, quantity
        out = ahf.execute_decisions(b, {"NVDA": D("sell", held / 4)})
        self.assertEqual(out["NVDA"].tool, "sell_stock")
        self.assertAlmostEqual(float(self.mcp.calls[-1][1]["amount"]), held / 4, places=15)
        ahf.execute_decisions(b, {"NVDA": D("sell", 10)})  # more than it holds: everything it holds
        self.assertEqual(self.mcp.calls[-1], ("sell_stock", {"wallet": ME, "symbol": "NVDA", "all": True}))
        self.assertEqual(ahf.execute_decisions(b, {"NVDA": D("cover", 0)})["NVDA"].tool, "close_short")
        self.assertIn("unknown action", ahf.execute_decisions(b, {"NVDA": D("yolo", 1)})["NVDA"])


class Desk:
    """A made-up wallet on a made-up Borro, answering the five tools the engine's broker uses in the shapes recorded
    from the real server. An order changes the book only once its transactions were sent through ``chain``."""

    FEE = 0.01  # Borro's fee on a spot swap, taken from the USDG

    def __init__(self, usdg=1000.0, held=None, shorts=None, longs=None, fail_sim=False, shortfall=0.0, windfall=0.0):
        # held: stock tokens in the wallet; shorts: [owed stock, held USDG]; longs: leveraged longs, [posted stock, owed USDG]
        self.usdg, self.held, self.shorts, self.longs = usdg, dict(held or {}), dict(shorts or {}), dict(longs or {})
        self.fail_sim, self.shortfall, self.windfall = fail_sim, shortfall, windfall  # windfall: USDG that lands in the wallet beside an order
        stocks = fx("markets")["result"]["structuredContent"]["stocks"]
        self.price = {x["symbol"]: x["price_usd"] for x in stocks}
        self.token = {x["symbol"]: x["contracts"]["token"] for x in stocks}
        self.chain, self._seen, self._pending = None, 0, None

    def answers(self):
        return {"position": self.position, "buy_stock": self.buy_stock, "sell_stock": self.sell_stock, "open_short": self.open_short, "close_short": self.close_short}

    def _answer(self, name, quote, then, token=None):
        self._pending = then
        a = fx(name)
        a["result"]["structuredContent"]["quote"] = quote
        if token:  # the recorded sale is NVDA's: the same steps for the stock asked for
            t = a["result"]["structuredContent"]["transactions"]
            t[0]["to"] = token
            t[1]["data"] = t[1]["data"][:34] + token[2:].lower() + t[1]["data"][74:]
        if self.fail_sim:
            a["result"]["structuredContent"]["simulation"]["ok"] = False
        return a

    @staticmethod
    def _refuse(text):
        return {"jsonrpc": "2.0", "id": 1, "result": {"isError": True, "content": [{"type": "text", "text": text}]}}

    def position(self, args):
        if self.chain is not None and len(self.chain.sent) > self._seen and self._pending:
            self._pending()
            self.usdg += self.windfall
        self._seen, self._pending = (len(self.chain.sent) if self.chain is not None else 0), None
        a = fx("position_all")
        s = a["result"]["structuredContent"]
        s["wallet_balances"]["usdg"] = round(self.usdg, 6)
        s["leveraged_longs"] = [{"symbol": k, "size_usdg": v[0] * self.price[k], "owed_usdg": v[1]} for k, v in self.longs.items()]
        s["stocks"] = [{"symbol": k, "price_usd": self.price[k],
                        **({"in_wallet": self.held[k]} if self.held.get(k) else {}),
                        **({"usdg_loan": {"posted_stock": self.longs[k][0], "owed_usdg": self.longs[k][1]}} if k in self.longs else {}),
                        **({"short": {"owed_stock": self.shorts[k][0], "owed_usd": self.shorts[k][0] * self.price[k], "held_usdg": self.shorts[k][1]}} if k in self.shorts else {})}
                       for k in sorted(set(self.held) | set(self.longs) | set(self.shorts))]
        return a

    def buy_stock(self, args):
        sym, usdg = args["symbol"], float(args["usdg"])
        if usdg > self.usdg:
            return self._refuse(f"the wallet holds {self.usdg} USDG, less than the USDG to spend")
        pool = self.price[sym] * 1.002           # the pool sits a little over Borro's price
        stock = usdg * (1 - self.FEE) / pool     # all the USDG is spent: the fee, then the swap

        def then():
            self.held[sym] = self.held.get(sym, 0.0) + stock * (1 - self.shortfall)
            self.usdg -= usdg
        return self._answer("buy_stock", {"usdg_in": args["usdg"], "stock": repr(stock), "least_accepted_stock": repr(stock * 0.995), "fee_usdg": repr(usdg * self.FEE), "fee_bps": 100}, then)

    def sell_stock(self, args):
        sym, have = args["symbol"], self.held.get(args["symbol"], 0.0)
        if not have > 0:
            return self._refuse(f"this wallet holds no {sym} to sell")
        shares = have if args.get("all") else float(args["amount"])
        if shares > have:
            return self._refuse(f"the wallet holds {have} {sym}, less than the amount")
        sale = shares * self.price[sym] * 0.998  # the pool pays a little under Borro's price
        net = sale * (1 - self.FEE)

        def then():
            self.usdg += net
            if shares == have:
                del self.held[sym]
            else:
                self.held[sym] = have - shares
        return self._answer("sell_stock", {"stock_in": repr(shares), "sale_usdg": repr(sale), "after_fee_usdg": repr(net), "least_accepted_usdg": repr(net * 0.995), "fee_usdg": repr(sale * self.FEE), "fee_bps": 100},
                            then, token=self.token[sym])

    def open_short(self, args):
        sym, amount, deposit = args["symbol"], float(args["amount"]), float(args.get("deposit", 0))
        net = amount * self.price[sym] * 0.998 * 0.999

        def then():
            owed, held = self.shorts.get(sym, [0.0, 0.0])
            self.shorts[sym] = [owed + amount, held + net + deposit]
            self.usdg -= deposit
        return self._answer("open_short", {"after_fee_usdg": repr(net), "deposit_usdg": args.get("deposit"), "deposit_at_least_usdg": round(0.6 * net, 2), "held_usdg": net + deposit}, then)

    def close_short(self, args):
        sym = args["symbol"]
        if sym not in self.shorts:
            return self._refuse(f"this wallet has no short on {sym}")
        owed, held = self.shorts[sym]

        def then():
            self.usdg += held - owed * self.price[sym] * 1.003
            del self.shorts[sym]
        return self._answer("close_short", {"top_up_usdg": "0.0"}, then)


def order(ticker, side, quantity):
    return {"ticker": ticker, "side": side, "quantity": quantity, "price": 1.0}


order_of = order


class TestEngineBroker(Base):
    """BorroBroker, ai-hedge-fund's v2 ``Broker`` protocol."""

    def desk(self, live=True, simulate_fills=False, caps=True, **kw):
        self.d = Desk(**kw)
        caps = {"max_order_usdg": 5000, "max_day_usdg": 20000} if caps else {}
        b = self.broker(live=live, mcp=FakeMcp(**self.d.answers()), **caps)
        self.d.chain = self.chain
        return ahf.BorroBroker(b, simulate_fills=simulate_fills)

    def test_whole_shares_and_cash_counted_as_the_engine_counts_them(self):
        b = self.desk(usdg=500.0, held={"NVDA": 2.0004}, shorts={"TSLA": [1.0000001, 760.0]})
        self.assertEqual({k: (v.ticker, v.shares) for k, v in b.positions().items()}, {"NVDA": ("NVDA", 2), "TSLA": ("TSLA", -1)})
        self.assertAlmostEqual(b.cash(), 500.0 + 760.0, places=6)  # the wallet, plus the money held for the shorts; a stock in the wallet owes nothing
        self.assertEqual(self.mcp.names(), ["position"])  # one read served both
        self.assertEqual(b.exact(), {"shares": {"NVDA": 2.0004, "TSLA": -1.0000001}, "cash": b.cash()})
        self.assertEqual(self.mcp.names(), ["position", "position"])
        b._seen = (b._seen[0] - 60, b._seen[1])  # a read is kept for seconds, not for the next session
        b.cash()
        self.assertEqual(self.mcp.names(), ["position"] * 3)
        self.assertEqual(self.desk(usdg=7.0, held={"NVDA": 0.004}).positions(), {})  # under half a share is no row

    def test_a_holding_and_a_short_on_one_stock_net(self):
        b = self.desk(held={"NVDA": 3.0}, shorts={"NVDA": [1.0, 340.0]})
        self.assertEqual(b.positions()["NVDA"].shares, 2)

    def test_a_leveraged_long_is_outside_the_book(self):
        # opened before 0.2.0 (a buy was a long at 1.01x then) or elsewhere: neither its stock nor its loan is counted
        b = self.desk(usdg=500.0, longs={"NVDA": [2.0, 3.4]})
        self.assertEqual((b.positions(), b.cash()), ({}, 500.0))
        self.assertEqual([p["symbol"] for p in b.broker.positions()["longs"]], ["NVDA"])  # the broker still lists it, for close_long

    def test_a_live_buy_gains_the_whole_count_and_is_priced_by_the_cash_it_moved(self):
        b = self.desk()
        before = b.cash()
        fill = b.place_order(order("nvda", "buy", 3))
        self.assertEqual(self.mcp.names(), ["position", "markets", "position", "buy_stock", "buy_stock", "position"])
        first, second = (c[1] for c in self.mcp.calls if c[0] == "buy_stock")
        self.assertAlmostEqual(float(first["usdg"]), 3 * self.d.price["NVDA"], places=5)
        self.assertNotEqual(first["usdg"], second["usdg"])  # scaled by what the first quote said the USDG buys
        self.assertAlmostEqual(self.d.held["NVDA"], 3, places=6)
        self.assertEqual((fill.ticker, fill.side, fill.quantity), ("NVDA", "buy", 3))
        self.assertAlmostEqual(fill.price, (before - b.cash()) / 3, places=9)
        self.assertAlmostEqual(fill.price, self.d.price["NVDA"] * 1.002 / 0.99, places=3)  # the pool's price with Borro's fee in it, not the order's
        self.assertEqual(b.positions()["NVDA"].shares, 3)
        self.assertTrue(b.results[0].sent and len(self.chain.sent) == 3)
        self.assertEqual((self.d.longs, self.d.shorts), ({}, {}))  # nothing lent, posted or borrowed
        self.assertAlmostEqual(b.cash(), self.d.usdg, places=6)

    def test_a_live_sell_sells_the_count_and_the_last_whole_shares_sell_everything_held(self):
        b = self.desk(held={"NVDA": 3.0001})
        before = b.cash()
        fill = b.place_order(order("NVDA", "sell", 1))
        self.assertEqual(self.mcp.calls[-2], ("sell_stock", {"wallet": ME, "symbol": "NVDA", "amount": "1"}))
        self.assertAlmostEqual(fill.price, b.cash() - before, places=9)
        self.assertAlmostEqual(fill.price, self.d.price["NVDA"] * 0.998 * 0.99, places=6)  # the sale after Borro's fee, per share
        self.assertEqual(b.positions()["NVDA"].shares, 2)
        b.place_order(order("NVDA", "sell", 2))
        self.assertEqual(self.mcp.calls[-2], ("sell_stock", {"wallet": ME, "symbol": "NVDA", "all": True}))  # the 0.0001 goes too: nothing is left behind
        self.assertEqual((b.positions(), self.d.held), ({}, {}))
        self.assertAlmostEqual(b.cash(), self.d.usdg, places=6)
        under = self.desk(held={"NVDA": 1.9998})  # a hair under two whole shares is still two: selling both sells what is there
        under.place_order(order("NVDA", "sell", 2))
        self.assertEqual((self.mcp.calls[-2][1].get("all"), under.positions()), (True, {}))

    def test_orders_borro_cannot_fill_whole_are_refused_before_anything_is_asked(self):
        b = self.desk(held={"NVDA": 300.0}, shorts={"TSLA": [3.0, 2300.0]})
        for o, why in ((order("NVDA", "sell", 301), "sell the 300 NVDA held first, then short"),
                       (order("TSLA", "buy", 1), "closes a short whole: buy 3 TSLA"), (order("TSLA", "buy", 4), "closes a short whole"),
                       (order("NVDA", "hold", 1), "buy or sell"), (order("NVDA", "buy", 0), "at least one share")):
            with self.assertRaisesRegex(BorroLiveError, why):
                b.place_order(o)
        with self.assertRaises(UnlistedTicker):
            b.place_order(order("DOGE", "buy", 1))
        self.assertTrue(set(self.mcp.names()) <= {"position", "markets"})
        self.assertEqual((self.chain.sent, b.results), ([], []))

    def test_one_share_of_three_hundred_sells_with_no_minimum(self):
        b = self.desk(held={"NVDA": 300.0})
        b.place_order(order("NVDA", "sell", 1))  # a long at 1.01x could not close under 1% of itself; a spot sale has no such floor
        self.assertEqual(b.positions()["NVDA"].shares, 299)

    def test_a_live_short_counts_the_money_borro_holds_and_a_cover_closes_it_whole(self):
        b = self.desk()
        before = b.cash()
        fill = b.place_order(order("TSLA", "sell", 2))
        self.assertEqual(self.mcp.names()[-3:], ["open_short", "open_short", "position"])
        self.assertAlmostEqual(float(self.mcp.calls[-2][1]["amount"]), 2, places=12)
        self.assertEqual(b.positions()["TSLA"].shares, -2)
        self.assertAlmostEqual(fill.price, self.d.price["TSLA"] * 0.998 * 0.999, places=6)  # the sale after Borro's fee, per share
        self.assertAlmostEqual(b.cash(), before + 2 * fill.price, places=6)  # cash rose by the sale, though the wallet paid the deposit in
        self.assertLess(self.d.usdg, 1000 - 2 * 370)
        cover = b.place_order(order("TSLA", "buy", 2))
        self.assertEqual(self.mcp.names()[-2:], ["close_short", "position"])
        self.assertAlmostEqual(cover.price, self.d.price["TSLA"] * 1.003, places=6)
        self.assertEqual(b.positions(), {})
        self.assertAlmostEqual(b.cash(), before + 2 * fill.price - 2 * cover.price, places=6)

    def test_a_fill_that_did_not_move_the_book_by_the_order_raises(self):
        b = self.desk(shortfall=0.4)
        with self.assertRaisesRegex(ahf.FillMismatch, r"buy 3 NVDA was sent \(0x[0-9a-f]{64}.*moved by \+2 shares.*holds 1\.8 NVDA now"):
            b.place_order(order("NVDA", "buy", 3))
        self.assertTrue(b.results[0].sent)  # the order is on record, with its hashes
        b = self.desk(windfall=5000.0)  # the shares came, but money reached the wallet from elsewhere while the order ran
        with self.assertRaisesRegex(ahf.FillMismatch, r"buy 1 NVDA was sent .*moved by \+1 shares and \+48\d\d\.\d+ USDG"):
            b.place_order(order("NVDA", "buy", 1))

    def test_a_whole_share_over_a_cap_names_the_order_the_cap_and_how_to_raise_it(self):
        b = self.desk(caps=False)  # 10 USDG an order, 25 a day
        with self.assertRaises(CapExceeded) as e:
            b.place_order(order("NVDA", "buy", 1))
        for part in ("buy 1 NVDA is about 169.36 USDG", "per-order cap of 10 (max_order_usdg)", "whole shares", "Broker(max_order_usdg=..., max_day_usdg=...)", "BORRO_MAX_ORDER_USDG and BORRO_MAX_DAY_USDG"):
            self.assertIn(part, str(e.exception))
        with self.assertRaisesRegex(CapExceeded, "sell 1 TSLA is about 378.19 USDG.*per-order cap of 10"):
            b.place_order(order("TSLA", "sell", 1))
        self.assertTrue(set(self.mcp.names()) <= {"position", "markets"})  # refused before Borro was asked for a quote
        d = Desk()
        day = ahf.BorroBroker(self.broker(live=True, mcp=FakeMcp(**d.answers()), max_order_usdg=1000, max_day_usdg=200))
        with self.assertRaisesRegex(CapExceeded, "buy 2 NVDA is about 338.72 USDG.*today's cap of 200.*max_day_usdg"):
            day.place_order(order("NVDA", "buy", 2))
        self.assertEqual(self.chain.sent, [])

    def test_a_dry_run_raises_with_the_simulation_unless_simulated_fills_are_asked_for(self):
        b = self.desk(live=False)
        with self.assertRaisesRegex(ahf.DryRun, "simulated \\(ok\\) and not sent.*simulate_fills=True") as e:
            b.place_order(order("NVDA", "buy", 1))
        self.assertTrue(e.exception.result.ok and not e.exception.result.sent)
        self.assertEqual((b.positions(), b.cash(), self.chain.sent), ({}, 1000.0, []))
        self.assertFalse(b.simulate_fills)

    def test_simulated_fills_are_refused_on_a_live_broker(self):
        with self.assertRaisesRegex(BorroLiveError, "simulate_fills is for a dry run"):
            self.desk(live=True, simulate_fills=True)

    def test_simulated_fills_keep_a_paper_book_and_send_nothing(self):
        b = self.desk(live=False, simulate_fills=True)
        fill = b.place_order(order("NVDA", "buy", 2))
        q = b.results[0].quote
        self.assertAlmostEqual(fill.price, float(q["usdg_in"]) / 2, places=9)  # the USDG the order spends, fee and all
        self.assertAlmostEqual(float(q["stock"]), 2, places=6)
        self.assertEqual(b.positions()["NVDA"].shares, 2)
        self.assertAlmostEqual(b.cash(), 1000 - 2 * fill.price, places=9)
        sold = b.place_order(order("NVDA", "sell", 1))  # these shares exist only on paper: Borro has nothing to simulate
        self.assertEqual(sold.price, self.d.price["NVDA"])
        self.assertIn("not simulated", b.results[1].note)
        self.assertFalse(b.results[1].ok or b.results[1].sent)
        self.assertNotIn("sell_stock", self.mcp.names())
        with self.assertRaisesRegex(BorroLiveError, "sell the 1 NVDA held first"):
            b.place_order(order("NVDA", "sell", 2))
        b.place_order(order("NVDA", "sell", 1))
        self.assertEqual(b.positions(), {})
        short = b.place_order(order("TSLA", "sell", 1))
        self.assertAlmostEqual(short.price, float(b.results[-1].quote["after_fee_usdg"]), places=9)
        self.assertEqual(b.positions()["TSLA"].shares, -1)
        with self.assertRaisesRegex(BorroLiveError, "closes a short whole"):
            b.place_order(order("TSLA", "buy", 2))
        cover = b.place_order(order("TSLA", "buy", 1))
        self.assertEqual(cover.price, self.d.price["TSLA"])
        self.assertIn("not simulated", b.results[-1].note)
        self.assertEqual(b.positions(), {})
        self.assertAlmostEqual(b.cash(), 1000 - 2 * fill.price + 2 * sold.price + short.price - cover.price, places=9)
        self.assertEqual((self.chain.sent, b.exact()), ([], {"shares": {}, "cash": 1000.0}))  # the wallet never moved
        self.assertTrue(all(not r.sent and not r.live for r in b.results))

    def test_simulated_closes_of_what_the_wallet_really_holds_are_borros_simulations(self):
        b = self.desk(live=False, simulate_fills=True, held={"NVDA": 2.0}, shorts={"TSLA": [1.0, 760.0]})
        sold = b.place_order(order("NVDA", "sell", 1))
        self.assertEqual(self.mcp.calls[-1], ("sell_stock", {"wallet": ME, "symbol": "NVDA", "amount": "1"}))
        q = b.results[0].quote
        self.assertAlmostEqual(sold.price, float(q["after_fee_usdg"]), places=9)  # what the quote says the share brings, Borro's fee taken
        self.assertTrue(b.results[0].ok)
        cover = b.place_order(order("TSLA", "buy", 1))
        self.assertEqual(self.mcp.names()[-1], "close_short")
        self.assertEqual(cover.price, self.d.price["TSLA"])
        self.assertIn("carries no cost", b.results[1].note)
        self.assertEqual({k: v.shares for k, v in b.positions().items()}, {"NVDA": 1})
        self.assertEqual(b.exact()["shares"], {"NVDA": 2.0, "TSLA": -1.0})

    def test_a_failed_simulation_gives_no_simulated_fill(self):
        b = self.desk(live=False, simulate_fills=True, fail_sim=True)
        with self.assertRaisesRegex(SimulationFailed, "buy 1 NVDA: Borro's simulation failed"):
            b.place_order(order("NVDA", "buy", 1))
        self.assertEqual((b.positions(), b.cash()), ({}, 1000.0))

    def test_the_engines_own_models_when_it_is_installed(self):
        b = self.desk(held={"NVDA": 2.0})
        try:
            from hedge_fund.brokers.models import Fill, Order, Position
            from hedge_fund.brokers.protocol import Broker as EngineBroker
        except ImportError:
            self.skipTest("ai-hedge-fund's v2 engine (aihf) is not installed")
        self.assertIsInstance(b, EngineBroker)
        self.assertIsInstance(b.positions()["NVDA"], Position)
        self.assertIsInstance(b.place_order(Order(ticker="NVDA", side="sell", quantity=2, price=170.0)), Fill)


class TestCli(Base):
    def test_the_command_line_is_a_dry_run_and_prints_no_key(self):
        import contextlib
        import io
        from unittest import mock
        os.environ.update(BORRO_AGENT_KEY=KEY, BORRO_LIVE_STATE=str(self.state))
        out = io.StringIO()
        with mock.patch("borro_live.broker.McpClient", lambda: McpClient(transport=FakeMcp())), contextlib.redirect_stdout(out):
            self.assertEqual(cli(["buy", "NVDA", "5"]), 0)
            self.assertEqual(cli(["sell", "NVDA"]), 0)
            self.assertEqual(cli(["close-long", "NVDA", "50"]), 0)
            self.assertEqual(cli(["positions"]), 0)
            self.assertEqual(cli(["buy", "DOGE", "5"]), 1)
            self.assertEqual(cli(["buy", "NVDA", "5", "--live"]), 1)  # the flag without BORRO_LIVE=1
        text = out.getvalue()
        self.assertIn('"sent": false', text)
        for part in ('"tool": "buy_stock"', '"tool": "sell_stock"', '"tool": "close_long"', '"holdings"'):
            self.assertIn(part, text)
        self.assertIn("BORRO_LIVE=1", text)
        self.assertNotIn(KEY[2:], text)


class KeylessDryRun(unittest.TestCase):
    """A dry run with no key: BORRO_WALLET stands in for the wallet, and every error is one family."""

    def test_from_env_reads_borro_wallet(self):
        import os
        from unittest import mock
        from borro_live import Broker, BorroLiveError
        with mock.patch.dict(os.environ, {"BORRO_WALLET": "0x" + "1" * 40}, clear=False):
            os.environ.pop("BORRO_AGENT_KEY", None)
            os.environ.pop("BORRO_LIVE", None)
            b = Broker.from_env()
            self.assertEqual(b.wallet, "0x" + "1" * 40)
            self.assertFalse(b.live)
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(BorroLiveError):
                Broker.from_env()

    def test_one_error_family(self):
        from borro_live import BorroLiveError, McpError, ToolRefused
        self.assertTrue(issubclass(McpError, BorroLiveError))
        self.assertTrue(issubclass(ToolRefused, BorroLiveError))


if __name__ == "__main__":
    unittest.main()
