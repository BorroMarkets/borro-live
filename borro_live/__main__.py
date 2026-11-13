"""borro-live on the command line: one order, or the wallet's positions, printed as JSON.

    borro-live tickers
    borro-live positions
    borro-live buy NVDA 5            a dry run of a spot buy of 5 USDG: Borro's simulation, nothing sent
    borro-live buy NVDA 5 --live     sends, and only with BORRO_LIVE=1 in the environment as well
    borro-live sell NVDA [percent] | short NVDA 5 | cover NVDA | lend USDG 5 | close-long NVDA [percent]
"""

from __future__ import annotations

import argparse
import json
import sys

from .broker import BorroLiveError, Broker
from .chain import ChainError
from .mcp import McpClient, McpError


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="borro-live", description="One order on Borro, dry run unless --live and BORRO_LIVE=1.")
    ap.add_argument("action", choices=["tickers", "positions", "buy", "sell", "short", "cover", "lend", "close-long"])
    ap.add_argument("symbol", nargs="?")
    ap.add_argument("amount", nargs="?", type=float, help="USDG for buy and short; percent for sell and close-long; units for lend")
    ap.add_argument("--live", action="store_true", help="send for real (also needs BORRO_LIVE=1)")
    ap.add_argument("--wallet", help="an address, for dry runs without a key")
    ap.add_argument("--builder-code")
    ap.add_argument("--max-order", type=float)
    ap.add_argument("--max-day", type=float)
    a = ap.parse_args(argv)
    try:
        if a.action == "tickers":   # a public read: no key and no wallet needed
            print(json.dumps([s["symbol"] for s in McpClient().call("markets")["stocks"]], indent=1))
            return 0
        b = Broker(live=a.live, wallet=a.wallet, builder_code=a.builder_code, max_order_usdg=a.max_order, max_day_usdg=a.max_day)
        if a.action == "positions":
            out = {k: v for k, v in b.positions().items() if k != "raw"}
        else:
            if not a.symbol or (a.amount is None and a.action in ("buy", "short", "lend")):
                ap.error(f"{a.action} needs a symbol" + (" and an amount" if a.action in ("buy", "short", "lend") else ""))
            if a.action in ("sell", "close-long"):
                out = getattr(b, a.action.replace("-", "_"))(a.symbol, a.amount if a.amount is not None else 100).to_dict()
            elif a.action == "cover":
                out = b.cover(a.symbol).to_dict()
            else:
                out = getattr(b, a.action)(a.symbol, a.amount).to_dict()
    except (BorroLiveError, McpError, ChainError, ValueError) as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
