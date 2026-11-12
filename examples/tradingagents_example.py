"""TradingAgents, live on Borro.

    python examples/tradingagents_example.py NVDA 2026-09-01              run TradingAgents, then a dry run of its decision
    python examples/tradingagents_example.py NVDA --decision Buy          skip the model: act on a decision you give
    BORRO_LIVE=1 python examples/tradingagents_example.py NVDA --decision Buy --live

Environment: BORRO_AGENT_KEY (the agent's own key), BORRO_RPC_URL (your Robinhood Chain RPC), and BORRO_LIVE=1 with
--live to send. Running the model also needs TradingAgents installed and its own model key.
"""

import argparse
import json

from borro_live import Broker
from borro_live.tradingagents import execute, go_live

ap = argparse.ArgumentParser()
ap.add_argument("ticker")
ap.add_argument("date", nargs="?", default="2026-09-01")
ap.add_argument("--decision", help="Buy, Overweight, Hold, Underweight or Sell: act on this instead of running the model")
ap.add_argument("--usdg", type=float, default=5.0, help="the size of a Buy, in USDG")
ap.add_argument("--allow-short", action="store_true", help="let a Sell with no long open a short")
ap.add_argument("--live", action="store_true")
a = ap.parse_args()

broker = Broker(live=a.live)
print(f"{'LIVE' if broker.live else 'dry run'} from {broker.wallet}; caps {broker.max_order_usdg:g} USDG an order, {broker.max_day_usdg:g} a day")

if a.decision:
    decision, orders = a.decision, execute(broker, a.ticker, a.decision, usdg=a.usdg, allow_short=a.allow_short)
else:
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    ta = go_live(TradingAgentsGraph(config=DEFAULT_CONFIG.copy()), broker, usdg=a.usdg, allow_short=a.allow_short)
    _, decision = ta.propagate(a.ticker, a.date)
    orders = ta.borro_orders
    if ta.borro_error:
        print("not ordered:", ta.borro_error)

print("decision:", decision)
for o in orders:
    print(f"{o.action} {o.symbol}: {'sent' if o.sent else 'simulated ' + ('ok' if o.ok else 'and it would fail')}")
    for t in o.transactions:
        print("  ", t.get("hash") or "(not sent)", t["kind"], t["description"])
    print("  ", json.dumps(o.quote))
if not orders:
    print("nothing to do")
print("positions:", json.dumps({k: v for k, v in broker.positions().items() if k not in ("raw", "stocks")}, indent=1))
