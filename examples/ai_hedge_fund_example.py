"""ai-hedge-fund, live on Borro.

    python examples/ai_hedge_fund_example.py --decisions decisions.json --scale 0.01
    python examples/ai_hedge_fund_example.py --buy NVDA 3                 one buy decision worth 3 USDG, as a dry run
                                                                          (a plain spot order: the token lands in the wallet)
    BORRO_LIVE=1 python examples/ai_hedge_fund_example.py --buy NVDA 3 --live

decisions.json is the portfolio manager's output: {"NVDA": {"action": "buy", "quantity": 10}, ...} (or the whole
run_hedge_fund result, with it under "decisions"). A buy and a sell are plain spot orders: USDG for the stock token in
the wallet, and back. Environment: BORRO_AGENT_KEY, BORRO_RPC_URL, and BORRO_LIVE=1 with --live to send.
"""

import argparse
import json

from borro_live import Broker
from borro_live.ai_hedge_fund import execute_decisions

ap = argparse.ArgumentParser()
ap.add_argument("--decisions", help="a JSON file of the portfolio manager's decisions")
ap.add_argument("--scale", type=float, default=1.0, help="multiply every quantity (0.01 turns 10 shares into 0.1)")
ap.add_argument("--buy", nargs=2, metavar=("TICKER", "USDG"), help="instead of a file: one buy decision of this many USDG")
ap.add_argument("--live", action="store_true")
a = ap.parse_args()

broker = Broker(live=a.live)
print(f"{'LIVE' if broker.live else 'dry run'} from {broker.wallet}; caps {broker.max_order_usdg:g} USDG an order, {broker.max_day_usdg:g} a day")

if a.buy:
    ticker, usdg = a.buy[0].upper(), float(a.buy[1])
    decisions = {ticker: {"action": "buy", "quantity": usdg / broker.price(ticker), "confidence": 0, "reasoning": "example"}}
elif a.decisions:
    with open(a.decisions) as f:
        decisions = json.load(f)
else:
    ap.error("give --decisions FILE or --buy TICKER USDG")

for ticker, o in execute_decisions(broker, decisions, scale=a.scale).items():
    if o is None:
        print(f"{ticker}: hold")
    elif isinstance(o, str):
        print(f"{ticker}: {o}")
    else:
        print(f"{ticker}: {o.action} {'sent' if o.sent else 'simulated ' + ('ok' if o.ok else 'and it would fail')}")
        for t in o.transactions:
            print("  ", t.get("hash") or "(not sent)", t["kind"], t["description"])
        print("  ", json.dumps(o.quote))
print("positions:", json.dumps({k: v for k, v in broker.positions().items() if k not in ("raw", "stocks")}, indent=1))
