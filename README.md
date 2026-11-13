# Go Live on Borro

`borro-live` sends a trading agent's decisions to [Borro](https://borro.markets) for real: buy, sell, short, cover and
lend tokenized stocks against USDG on Robinhood Chain. It comes with adapters for
[TradingAgents](https://github.com/TauricResearch/TradingAgents) and
[ai-hedge-fund](https://github.com/virattt/ai-hedge-fund), which decide and simulate but place no orders.

It is a small Python package: one dependency (`eth-account`), no framework imported unless you have it. It talks to
Borro's public MCP server (`https://borro.markets/mcp`, no key), which builds each transaction and simulates it; the
package checks the simulation, signs with your agent's own key and sends through your own RPC.

**Read this first.**

- **Not advice.** This is plumbing. Nothing here tells you what to trade, and nothing here promises a return. You can
  lose everything you send.
- **What a stock token is.** A token on Robinhood Chain, issued by Robinhood Assets (Jersey) Limited, whose price
  follows a stock or fund. It is not the share: no vote, no brokerage account, no investor protection scheme. Its price
  on chain can sit away from the stock's, most of all while the stock market is shut.
- **A buy is a plain spot order.** Your USDG buys the stock token on Uniswap and the token arrives in the agent's own
  wallet. Nothing is lent and nothing is borrowed, so a buy cannot be liquidated. A sell swaps the token back to USDG.
  Borro takes a fee on each of the two swaps, out of the USDG, and every quote shows it.
- **A short can be liquidated.** If the stock rises far enough, Borro closes the short for you at a loss, and the
  deposit can be lost.
- **It starts as a dry run.** Nothing is sent until you ask for live mode twice: a flag in code and a variable in the
  environment.

## Five minutes

```sh
pip install borro-live                # or, from a clone: pip install -e .
```

Three environment variables:

```sh
export BORRO_AGENT_KEY=0x...          # the agent's own wallet key. Use a fresh wallet with little in it.
export BORRO_RPC_URL=https://...      # your own Robinhood Chain RPC (chain id 4663). There is no built-in one.
export BORRO_LIVE=1                   # leave this out until you mean it
```

To try it with no key at all, set `BORRO_WALLET=0x...` to any address in place of `BORRO_AGENT_KEY`: every order is
then a dry run simulated from that address, and nothing can be sent. A simulation passes only if that address holds
the USDG the order needs.

Dry run (leave `BORRO_LIVE` out). It prints Borro's simulation of every step and sends nothing:

```sh
borro-live tickers
borro-live buy NVDA 5                 # a spot buy: 5 USDG of NVDA into the wallet
borro-live positions
```

Live. The wallet needs USDG for the order and a little ETH for gas:

```sh
BORRO_LIVE=1 borro-live buy NVDA 5 --live
BORRO_LIVE=1 borro-live sell NVDA --live
```

Each live order prints every transaction hash. A dry run needs no RPC and no key at all:
`borro-live buy NVDA 5 --wallet 0xYourAddress`.

## TradingAgents

`TradingAgentsGraph.propagate(ticker, date)` returns `(final_state, signal)`, where the signal is Buy, Overweight, Hold,
Underweight or Sell (or REVIEW when the run reached no rating). One line wraps it:

```python
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG
from borro_live import Broker
from borro_live.tradingagents import go_live

ta = go_live(TradingAgentsGraph(config=DEFAULT_CONFIG.copy()), Broker.from_env(), usdg=5)
_, decision = ta.propagate("NVDA", "2026-09-01")
print(decision, ta.borro_orders, ta.borro_error)
```

`propagate` answers exactly as before. What the adapter does with each rating:

| Rating | Order |
|---|---|
| Buy | cover a short if there is one, then buy `usdg` worth |
| Overweight | the same, half the size |
| Hold, REVIEW | nothing |
| Underweight | sell half of the stock the wallet holds, if it holds any |
| Sell | sell all of it. With `allow_short=True` and none held, short `usdg` worth instead |

Without the graph: `borro_live.tradingagents.execute(broker, "NVDA", "Buy", usdg=5)`. Runnable:
`python examples/tradingagents_example.py NVDA --decision Buy`.

## ai-hedge-fund

The portfolio manager's decisions (`{ticker: {"action": "buy" | "sell" | "short" | "cover" | "hold", "quantity":
shares}}`, what `run_hedge_fund` returns under `"decisions"`) go straight in:

```python
from borro_live import Broker
from borro_live.ai_hedge_fund import execute_decisions

results = execute_decisions(Broker.from_env(), result["decisions"], scale=0.01)
```

`scale` multiplies every quantity, since the model sizes in whole shares for a paper portfolio and the caps here are
in USDG. Pick it so the largest order lands under your per-order cap (10 USDG unless you raise it): an order over the
cap is refused, not shrunk. A sell sells that many shares of what the wallet holds, or all of it when it holds fewer.
Each ticker gets its own answer; one refusal never stops the others. Runnable:
`python examples/ai_hedge_fund_example.py --buy NVDA 3`.

### The v2 engine

The v2 engine (`hedge_fund/`, the `aihf` package) has a `Broker` protocol instead: `positions()`, `cash()` and
`place_order(order)`, whole shares on a signed book, fill completely or raise.
`borro_live.ai_hedge_fund.BorroBroker` satisfies it. The engine's own commands use its simulated and paper brokers
and take no other, so you call `advance` yourself and keep the state it hands back:

```python
from hedge_fund.fund import Fund, load_spec
from hedge_fund.pipeline.session import FundState, advance, next_state
from borro_live import Broker
from borro_live.ai_hedge_fund import BorroBroker

broker = BorroBroker(Broker.from_env(max_order_usdg=500, max_day_usdg=2000), simulate_fills=True)   # a dry run
fund = Fund(load_spec("fund.yaml"))
state = FundState.initial(broker.cash())          # the fund starts from what the wallet holds
for session in sessions:                          # completed sessions, oldest first
    record = advance(fund, state, session, broker, data_client, universe)
    state = next_state(state, record)
```

**The engine's own data client needs its own key** (`FINANCIAL_DATASETS_API_KEY`, or a wallet that pays per call
through x402). `borro-live` gives the engine no market data, and `data_client` above is yours to supply.

Treat this adapter as experimental. What it does, and where it stops:

- **Whole shares.** `positions()` is the stock tokens in the wallet, less the stock each short owes, rounded to
  whole shares. A buy is a spot order sized from Borro's quote so the wallet gains the whole count the engine ordered,
  to within a small fraction of a share. A sell sells exactly the count, or everything the wallet holds of the stock
  when the count is all its whole shares, so no fraction is left behind.
- **Cash.** `cash()` is the USDG in the wallet, plus the USDG Borro holds against the shorts (the sale money and the
  deposit). A buy of some shares therefore lowers cash by what those shares cost, and a short raises it by the sale
  after Borro's fee, as the engine's book expects. Stock you lent on Borro, and a leveraged long opened before 0.2.0
  or elsewhere, are outside this book.
- **Fills.** When a live order has landed, the book is read again. If it did not move by exactly the order,
  `place_order` raises `FillMismatch` with the transaction hashes. Otherwise the `Fill` carries the price the cash
  really moved by, per share, so the engine's fills account for its cash. Borro's fee and the pools' fees are in that
  price: on the fork test a buy filled up to 1.5% over Borro's price and a sell up to 1.5% under it.
- **Caps.** The engine orders whole shares, and one share of most stocks is worth more than the default 10 USDG
  cap, so set the caps to cover the orders you expect: `Broker(max_order_usdg=..., max_day_usdg=...)`
  or `BORRO_MAX_ORDER_USDG` and `BORRO_MAX_DAY_USDG`. An order over a cap is refused in words that name the order, its
  size and the cap. The day's cap counts every buy and short of the UTC day, so size it for a whole session: the
  engine stops a session at the first order that raises, with the orders before it already filled.
- **Dry run.** A broker that is not live sends nothing, and by default `place_order` raises `DryRun` with Borro's
  simulation, which stops the engine's session. With `simulate_fills=True` it returns a simulated `Fill` instead and
  keeps those fills in a paper book in memory, so whole sessions run end to end; `positions()` and `cash()` are then
  the wallet's real book plus the paper one. An order that opens or adds is priced from Borro's simulation of it from
  your wallet, so the wallet has to hold the USDG (a buy is priced at the USDG it spends, fee included; a sell of
  stock the wallet really holds at what its quote says it brings after the fee). An order that closes shares held only
  on paper cannot be simulated by Borro: it is priced at Borro's price, and its `Result` in `broker.results` says so.
  The paper book is gone when the process ends.
- **Refused, never half done.** An order that crosses zero in one step, and a buy that covers only part of a short
  (Borro closes a short whole). Each raises, and the engine stops there.
- **Leave room.** Borro's price is not the engine's mark. A mandate that targets all of its equity can ask for more
  USDG than the wallet holds, and Borro refuses that buy. The fork test runs at a gross target of 0.9.

**How far the engine's ledger reconciles.** Before each session the engine checks that the broker holds exactly the
whole shares, and the cash to the cent, that its last record says. A long-only fund on the fork test passed that
check on four sessions a day apart: a day on, cash was the same to six decimals and every holding was within 0.001
of a whole share. A stock token held in the wallet does not move between sessions: nothing is lent and nothing is
owed. Two things still break the check:

- The USDG Borro holds against a short earns interest. On the fork, one share of MSTR short earned about 0.023 USDG
  in a day, which is over the cent, so a short carried to the next session raises the engine's `BookMismatch`.
  Booking it by hand clears it: `state = state.model_copy(update={"cash": broker.cash()})`.
- Anything else that moves the wallet (a transfer, a lend, a position Borro closed for you) is a mismatch too, as it
  should be. `broker.exact()` gives the unrounded shares and cash at any time.

What was tested, and how: the unit tests, and three steps of the fork test that run the engine itself (`Fund`,
`advance`, `build_orders` and `reconcile` at commit `5c553b5`, version 2.7.2) with `BorroBroker` on a local fork: a
dry run with simulated fills, four live sessions a day apart (buys, a trim, a close) and a short. Two stand-ins were
used there: daily bars from Yahoo through `yfinance` in place of the engine's data client, and a stub analyst with set
convictions in place of a model. Not tested: the engine's own data client, a model deciding the orders, minute funds,
long-short mandates, and anything on Robinhood Chain itself. Those fork steps sent nothing to the real chain.

## What each order really is

Worked out from the server's own `tools/list`:

| `Broker` | Borro tool | What happens |
|---|---|---|
| `buy(symbol, usdg)` | `buy_stock` | A plain spot order: all the USDG is spent on Uniswap, through the stock's own pools, and the stock token arrives in the wallet. Nothing is lent or borrowed. |
| `sell(symbol, percent=100)` | `sell_stock` | A plain spot order: that share of the stock token the wallet holds (or `shares=` exactly) is sold for USDG. |
| `short(symbol, usdg)` | `open_short` | The stock is borrowed and sold; the sale money and a USDG deposit are held. You pay a borrow fee while it is open. |
| `cover(symbol)` | `close_short` | Buys back what is owed, returns it, pays out what is left. Borro has one short per stock: this closes all of it. |
| `lend(asset, amount)` | `supply` | USDG earns interest; a stock token you hold earns lending fees. |
| `positions()` | `position` | `holdings` (the stock tokens in the wallet), shorts, what is lent, wallet balances, and `longs` (leveraged longs). |
| `buy_spot_with_eth(symbol, shares)` | `buy_with_eth` | A plain spot buy paid in the wallet's ETH. The token lands in the wallet. |
| `close_long(symbol, percent=100)` | `close_long` | Closes a leveraged long. `buy` does not open one; see "Coming from 0.1" below. |

**What a spot order costs.** Each buy and each sell is one swap. Borro's fee comes out of the USDG inside the swap, and
the pools take their own. Every answer's quote shows the fee (`fee_usdg`, `fee_bps`), the price the order really gets
with the fees in it (`average_price_usd`), how far that is from Borro's price (`vs_feed_pct`) and the least the order
accepts (`least_accepted_stock` or `least_accepted_usdg`). On the fork test, buying 5 USDG of NVDA and selling it
again brought back 4.87 USDG. A swap is exact in: all of the USDG (or all of the shares) is spent, and at least the
quote less the slippage allowed arrives (half a percent; `Broker(slip_bps=...)` changes it), or the swap reverts and
nothing is spent.

**The steps you sign.** A spot order is up to three transactions: the token's approval to Permit2 (Uniswap's approval
contract) for exactly the amount, Permit2's allowance for Uniswap's Universal Router for exactly the amount (it lapses
after 30 minutes), then the swap. The kit refuses a spot order whose steps go anywhere else.

**What is in the wallet is yours to move.** A token `buy` bought sits in the wallet and earns nothing there. `lend`
lends it on Borro, where it earns lending fees; stock that is lent is no longer in the wallet, so `sell` does not see
it until you take it back on Borro.

**Coming from 0.1.** Before 0.2.0 Borro's MCP server had no spot tools, so `buy` opened a leveraged long at 1.01x and
`sell` closed it. 0.2.0 needs a server that lists `buy_stock` and `sell_stock`; against one that does not, `buy` and
`sell` raise `SpotUnavailable` and send nothing. A long opened by an older version is still
there: `positions()["longs"]` lists it, and `close_long` (or `borro-live close-long NVDA`) closes it. `sell` does not
touch it, and `BorroBroker` does not count it. `Broker(leverage=...)` is gone.

`short` deposits 1 USDG for each USDG shorted by default (`short_deposit=1.0`), and never less than a tenth over the
least Borro asks. At that default a short is closed for you after a rise of roughly half; the quote in every answer
gives the exact price (`closed_for_you_at_price`). A thinner deposit is closed sooner.

## Safety

- **Dry run by default.** Without `live=True` and `BORRO_LIVE=1` together, an order returns the tool's own simulation
  and sends nothing. `live=True` without the variable refuses to start; the variable without the flag stays a dry run.
- **Caps you set.** `max_order_usdg` (default 10) is the most one buy, short or lend may be worth; `max_day_usdg`
  (default 25) is the most they may add up to in one UTC day, per wallet. Over either, the order is refused before
  anything is signed. The day's total is kept in `~/.borro-live/state.json` (or `BORRO_LIVE_STATE`), counted before the
  first transaction goes out. Selling and covering are never stopped by a cap. Set them in code or with
  `BORRO_MAX_ORDER_USDG` and `BORRO_MAX_DAY_USDG`.
- **Listed tickers only.** A ticker Borro does not list is refused before any trade tool is called.
- **A failed simulation is never sent.** Every step must pass Borro's simulation, and each step is run again from the
  wallet (`eth_call`, then a gas estimate) just before it is signed. Steps go in order; each receipt is checked before
  the next.
- **What is signed.** Only transactions for your wallet on chain 4663. No order sends ETH except
  `buy_spot_with_eth`, and never more than its quote. A USDG approval is refused if it is for more than the order.
  A spot order may approve the token it spends to Permit2, let Uniswap's Universal Router take it through Permit2,
  and swap on that router: a step that goes anywhere else is refused.
- **The key.** Read from `BORRO_AGENT_KEY`, kept in memory, never printed, logged or written. Errors never carry your
  RPC address.
- **Trust.** Borro's server builds the transactions. Read a dry run's `transactions` before going live: each has the
  contract it goes to and what it does. The `rules` tool lists Borro's contracts.

## Builder codes: a fee share for whoever ships the integration

Borro shares its own fees with whoever brings the business (the Bookrunners programme). Give the broker a code and
installs that carry it pay the code's owner:

```python
Broker.from_env(builder_code="yourcode")      # or BORRO_BUILDER_CODE=yourcode
```

- The wallet is bound to the code once, with one free signature (`bind_builder`, no transaction). From then on the
  code earns Borro's fee on the interest that wallet pays when it borrows (its shorts). A spot buy or sell borrows
  nothing, so it earns the code nothing.
- Lends go through the `BorroBuilders` contract with the code, so the code earns Borro's fee on what those deposits
  earn. The wallet keeps its vault shares.
- The share is all of Borro's fee for 12 months, half after, paid monthly in USDG to the wallet that registered the
  code. The user pays nothing extra. Fees float with demand: with no borrowing there is no fee to share.
- Register a code with your own wallet at [borro.markets/bookrunners](https://borro.markets/bookrunners). First come,
  first served; only that wallet can move it. One code per wallet: the first binding stands.

There is no default code. If you ship this in a framework, set yours.

## Tests

```sh
python -m unittest discover -s tests -p 'test_unit.py'        # 71 tests against recorded MCP answers, no network
```

Borro also runs the kit end to end on a local fork of Robinhood Chain in its own repository: a dry run, a live spot
buy, lend, short, cover and spot sell, both adapters, the caps, a builder code, and ai-hedge-fund's engine for four
sessions. It reads the chain to check that a buy takes exactly the order's USDG and leaves the stock token in the
wallet. `BORRO_MCP_URL` points the client at another MCP address, which is how that test reaches its local server.

`walk.sh` is the one real walk: one small live order per framework from a funded wallet. It asks before it sends.

## Not here

Options, perps and Borro's dark pool are not part of this kit.

MIT licence.
