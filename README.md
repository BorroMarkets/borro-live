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
- **A short can be liquidated.** If the stock rises far enough, Borro closes the short for you at a loss, and the
  deposit can be lost. A buy here is a long with a small loan against it, and it can be liquidated too if the token
  falls nearly to zero.
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
borro-live buy NVDA 5                 # 5 USDG of NVDA
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
| Underweight | sell half of the long, if there is one |
| Sell | sell all of the long. With `allow_short=True` and no long, short `usdg` worth instead |

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
cap is refused, not shrunk. Each ticker gets its own answer; one refusal never stops the others. Runnable:
`python examples/ai_hedge_fund_example.py --buy NVDA 3`.

The v2 engine (`hedge_fund/`, the `aihf` package) has a `Broker` protocol instead: `positions()`, `cash()` and
`place_order(order)`. `borro_live.ai_hedge_fund.BorroBroker(Broker.from_env())` satisfies it. Whole shares on a signed
book meet two limits on Borro: a short is closed whole, and an order may not cross zero in one step. Both are refused
in words, never half done. Treat it as experimental, for three reasons. The engine orders whole shares, so with the
default 10 USDG cap every order is refused: raise the caps to more than a share of what you trade. The engine expects
`place_order` to fill or raise, so in a dry run it raises with Borro's simulation and the engine's session stops there.
And the engine reconciles its ledger against whole shares, while a buy on Borro fills at the share count the swap
gave. Its own data client also needs its own key.

## What each order really is

Worked out from the server's own `tools/list`:

| `Broker` | Borro tool | What happens |
|---|---|---|
| `buy(symbol, usdg)` | `open_long` at 1.01x | Your USDG buys the stock token; it is lent (it earns lending fees) and posted; 1% of the order is borrowed against it. |
| `sell(symbol, percent=100)` | `close_long` | The token is sold, the small loan repaid, USDG comes back. |
| `short(symbol, usdg)` | `open_short` | The stock is borrowed and sold; the sale money and a USDG deposit are held. You pay a borrow fee while it is open. |
| `cover(symbol)` | `close_short` | Buys back what is owed, returns it, pays out what is left. Borro has one short per stock: this closes all of it. |
| `lend(asset, amount)` | `supply` | USDG earns interest; a stock token you hold earns lending fees. |
| `positions()` | `position` | Longs, shorts, what is lent, wallet balances. |
| `buy_spot_with_eth(symbol, shares)` | `buy_with_eth` | A plain spot buy paid in the wallet's ETH. The token lands in the wallet. |

**Borro's MCP server has no tool for a plain spot buy or sell of a stock token for USDG.** `buy` is therefore the
nearest thing that can also be sold back through Borro: a long at 1.01x. It behaves like holding the token, with two
differences you should know: the token is lent out while you hold it, and there is a loan of 1% of the order against
it, so the position can be liquidated if the token loses nearly all its value. `buy_spot_with_eth` is a true spot
buy, but it pays in ETH and no Borro tool sells the token back; lend it, or sell it elsewhere.

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
  code earns Borro's fee on the interest that wallet pays when it borrows (shorts and the loan under a buy).
- Lends go through the `BorroBuilders` contract with the code, so the code earns Borro's fee on what those deposits
  earn. The wallet keeps its vault shares.
- The share is all of Borro's fee for 12 months, half after, paid monthly in USDG to the wallet that registered the
  code. The user pays nothing extra. Fees float with demand: with no borrowing there is no fee to share.
- Register a code with your own wallet at [borro.markets/bookrunners](https://borro.markets/bookrunners). First come,
  first served; only that wallet can move it. One code per wallet: the first binding stands.

There is no default code. If you ship this in a framework, set yours.

## Tests

```sh
python -m unittest discover -s tests -p 'test_unit.py'        # 50 tests against recorded MCP answers, no network
```

Borro also runs the kit end to end on a local fork of Robinhood Chain in its own repository: a dry run, a live buy, lend,
short, cover and sell, both adapters, the caps and a builder code. `BORRO_MCP_URL` points the client at another MCP
address, which is how that test reaches its local server.

`walk.sh` is the one real walk: one small live order per framework from a funded wallet. It asks before it sends.

## Not here

Options, perps and Borro's dark pool are not part of this kit.

MIT licence.
