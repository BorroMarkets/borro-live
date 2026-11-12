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

from borro_live import Broker, CapExceeded, ChainError, McpClient, McpError, SimulationFailed, ToolRefused, UnlistedTicker, Unsafe  # noqa: E402
from borro_live import ai_hedge_fund as ahf  # noqa: E402
from borro_live import tradingagents as ta  # noqa: E402
from borro_live.__main__ import main as cli  # noqa: E402
from borro_live.broker import BORRO_BUILDERS, USDG, BorroLiveError  # noqa: E402
from borro_live.chain import Chain  # noqa: E402

FIX = Path(__file__).parent / "fixtures"
KEY = "0x" + "5a" * 32  # a made-up key: the wallet the answers were recorded for
ME = Account.from_key(KEY).address
SECRET_RPC = "https://rpc.example/v2/SECRET-RPC-KEY"


def fx(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


class FakeMcp:
    """Replays recorded answers by tool name; ``answers`` overrides one, and every call is kept."""

    DEFAULT = {"markets": "markets", "position": "position_all", "open_long": "open_long", "close_long": "close_long", "open_short": "open_short",
               "close_short": "close_short", "supply": "supply_usdg", "buy_with_eth": "buy_with_eth", "bind_builder": "bind_typed"}

    def __init__(self, **answers):
        self.answers = answers
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, payload: dict) -> dict:
        m = payload["method"]
        if m == "initialize":
            return fx("initialize")
        if m == "tools/list":
            return fx("tools_list")
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
        need = {"markets": [], "position": ["wallet"], "open_long": ["wallet", "symbol", "margin", "leverage"], "close_long": ["wallet", "symbol"],
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
        self.assertEqual((r.sent, r.ok, r.tool, r.symbol), (False, True, "open_long", "NVDA"))
        self.assertEqual([t["kind"] for t in r.transactions], ["approve", "authorize", "call"])
        self.assertEqual(r.simulation["method"], "eth_simulateV1")
        self.assertEqual(r.hashes, [])
        self.assertEqual(self.chain.sent, [])
        self.assertEqual(self.mcp.calls[-1], ("open_long", {"wallet": ME, "symbol": "NVDA", "margin": "5", "leverage": 1.01}))
        self.assertEqual(b.spent_today(), 0)

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
        prep = fx("open_long")
        prep["result"]["structuredContent"]["simulation"]["steps"].pop()
        with self.assertRaises(SimulationFailed):
            self.broker(live=True, mcp=FakeMcp(open_long=prep)).buy("NVDA", 5)
        self.assertEqual(self.chain.sent, [])

    def test_transactions_for_another_wallet_chain_or_with_eth_are_refused(self):
        def bent(change):
            prep = fx("open_long")
            change(prep["result"]["structuredContent"])
            return prep
        cases = [lambda s: s.update({"from": "0x" + "11" * 20}), lambda s: s.update(chainId=1), lambda s: s["transactions"][2].update(chainId=1),
                 lambda s: s["transactions"][2].update(value="1"), lambda s: s.update(transactions=[])]
        for change in cases:
            b = self.broker(live=True, mcp=FakeMcp(open_long=bent(change)))
            with self.assertRaises(Unsafe):
                b.buy("NVDA", 5)
            self.assertEqual(self.chain.sent, [])

    def test_a_usdg_approval_over_the_order_is_refused(self):
        prep = fx("open_long")
        t = prep["result"]["structuredContent"]["transactions"][0]
        self.assertEqual(t["to"], USDG)
        t["data"] = t["data"][:74] + "f" * 64 + t["data"][138:]
        with self.assertRaisesRegex(Unsafe, "approval"):
            self.broker(live=True, mcp=FakeMcp(open_long=prep)).buy("NVDA", 5)
        self.assertEqual(self.chain.sent, [])

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
        prep = fx("open_long")["result"]["structuredContent"]["transactions"]
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
        self.assertEqual(b.sell("NVDA", 50).tool, "close_long")
        self.assertEqual(self.mcp.calls[-1], ("close_long", {"wallet": ME, "symbol": "NVDA", "percent": 50.0}))
        self.assertEqual(b.cover("NVDA").tool, "close_short")
        self.assertEqual(self.mcp.calls[-1], ("close_short", {"wallet": ME, "symbol": "NVDA"}))
        r = b.lend("USDG", 5)
        self.assertEqual((r.tool, r.sent, len(r.hashes)), ("supply", True, 2))
        self.assertEqual(self.mcp.calls[-1], ("supply", {"wallet": ME, "asset": "USDG", "amount": "5"}))
        with self.assertRaisesRegex(BorroLiveError, "percent"):
            b.sell("NVDA", 0)

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
        self.assertEqual([x["symbol"] for x in p["longs"]], ["NVDA"])
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
        # the recorded open_long approves 5 USDG, so Overweight (half the size) is asked with 10
        for rating, size, want in [("Buy", 5, [("open_long", "5")]), ("Overweight", 10, [("open_long", "5")]), ("Hold", 5, []), ("Underweight", 5, []), ("Sell", 5, []), ("REVIEW", 5, [])]:
            b = self.broker(mcp=FakeMcp(**self.FLAT))
            out = ta.execute(b, "NVDA", rating, usdg=size)
            self.assertEqual([(r.tool, self.mcp.calls[-1][1].get("margin")) for r in out], want, rating)

    def test_each_rating_with_a_long_and_a_short_open(self):
        b = self.broker()
        self.assertEqual([r.tool for r in ta.execute(b, "NVDA", "Buy")], ["close_short", "open_long"])
        ta.execute(b, "NVDA", "Underweight")
        self.assertEqual(self.mcp.calls[-1], ("close_long", {"wallet": ME, "symbol": "NVDA", "percent": 50.0}))
        ta.execute(b, "NVDA", "SELL")
        self.assertEqual(self.mcp.calls[-1][1]["percent"], 100.0)

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
        self.assertEqual(out["NVDA"].tool, "open_long")
        self.assertAlmostEqual(out["NVDA"].usdg, 0.03 * b.price("NVDA"), places=6)
        self.assertEqual(out["TSLA"].tool, "open_short")
        self.assertIsNone(out["SPY"])
        self.assertIn("refused: DOGE is not on Borro", out["DOGE"])
        self.assertIn("refused: no long on QQQ", out["QQQ"])

    def test_whole_shares_meet_the_cap(self):
        out = ahf.execute_decisions(self.broker(), {"NVDA": {"action": "buy", "quantity": 3}})
        self.assertIn("refused", out["NVDA"])
        self.assertIn("per-order cap", out["NVDA"])

    def test_sell_is_a_share_of_the_long_and_cover_closes_the_short(self):
        b = self.broker()
        held = ahf._long_shares(b.positions(), "NVDA")

        class D:  # a decision object, as the portfolio manager's pydantic model
            def __init__(self, action, quantity):
                self.action, self.quantity = action, quantity
        out = ahf.execute_decisions(b, {"NVDA": D("sell", held / 4)})
        self.assertEqual(out["NVDA"].tool, "close_long")
        self.assertAlmostEqual(self.mcp.calls[-1][1]["percent"], 25, places=1)
        self.assertEqual(ahf.execute_decisions(b, {"NVDA": D("cover", 0)})["NVDA"].tool, "close_short")
        self.assertIn("unknown action", ahf.execute_decisions(b, {"NVDA": D("yolo", 1)})["NVDA"])

    def test_v2_broker_protocol(self):
        pos = fx("position_all")
        s = pos["result"]["structuredContent"]
        s["stocks"][0]["usdg_loan"]["posted_stock"] = 2.0004
        s["stocks"][0]["short"] = None
        live = ahf.BorroBroker(self.broker(live=True, mcp=FakeMcp(position=pos), max_order_usdg=1000, max_day_usdg=5000))
        book = live.positions()
        self.assertEqual({k: v.shares for k, v in book.items()}, {"NVDA": 2})
        self.assertGreater(live.cash(), 0)
        fill = live.place_order({"ticker": "NVDA", "side": "buy", "quantity": 1, "price": 170.0})
        self.assertEqual((fill.ticker, fill.side, fill.quantity), ("NVDA", "buy", 1))
        self.assertAlmostEqual(fill.price, 169.35, places=1)
        live.place_order({"ticker": "NVDA", "side": "sell", "quantity": 1, "price": 170.0})
        self.assertAlmostEqual(self.mcp.calls[-1][1]["percent"], 50, places=0)
        with self.assertRaisesRegex(BorroLiveError, "first, then short"):
            live.place_order({"ticker": "NVDA", "side": "sell", "quantity": 5, "price": 170.0})
        live.place_order({"ticker": "TSLA", "side": "sell", "quantity": 1, "price": 400.0})
        self.assertEqual(self.mcp.names()[-1], "open_short")
        dry = ahf.BorroBroker(self.broker(mcp=FakeMcp(position=pos), max_order_usdg=1000, max_day_usdg=5000))
        with self.assertRaises(ahf.DryRun) as e:
            dry.place_order({"ticker": "NVDA", "side": "buy", "quantity": 1, "price": 170.0})
        self.assertFalse(e.exception.result.sent)

    def test_v2_cover_is_whole(self):
        pos = fx("position_all")
        s = pos["result"]["structuredContent"]
        s["leveraged_longs"] = []
        s["stocks"][0]["short"]["owed_stock"] = 3.0
        b = ahf.BorroBroker(self.broker(live=True, mcp=FakeMcp(position=pos), max_order_usdg=1000))
        self.assertEqual(b.positions()["NVDA"].shares, -3)
        with self.assertRaisesRegex(BorroLiveError, "closes a short whole"):
            b.place_order({"ticker": "NVDA", "side": "buy", "quantity": 1, "price": 170.0})
        b.place_order({"ticker": "NVDA", "side": "buy", "quantity": 3, "price": 170.0})
        self.assertEqual(self.mcp.names()[-1], "close_short")


class TestCli(Base):
    def test_the_command_line_is_a_dry_run_and_prints_no_key(self):
        import contextlib
        import io
        from unittest import mock
        os.environ.update(BORRO_AGENT_KEY=KEY, BORRO_LIVE_STATE=str(self.state))
        out = io.StringIO()
        with mock.patch("borro_live.broker.McpClient", lambda: McpClient(transport=FakeMcp())), contextlib.redirect_stdout(out):
            self.assertEqual(cli(["buy", "NVDA", "5"]), 0)
            self.assertEqual(cli(["positions"]), 0)
            self.assertEqual(cli(["buy", "DOGE", "5"]), 1)
            self.assertEqual(cli(["buy", "NVDA", "5", "--live"]), 1)  # the flag without BORRO_LIVE=1
        text = out.getvalue()
        self.assertIn('"sent": false', text)
        self.assertIn("BORRO_LIVE=1", text)
        self.assertNotIn(KEY[2:], text)


if __name__ == "__main__":
    unittest.main()
