#!/usr/bin/env bash
# The one real walk: one small live order per framework, from a funded test wallet, on Robinhood Chain itself.
# It sends real transactions and spends real money (about 6 USDG in orders, plus gas). Run it once, by hand.
#
#   export BORRO_AGENT_KEY=0x...     a fresh test wallet's key, holding about 10 USDG and 0.001 ETH on Robinhood Chain
#   export BORRO_RPC_URL=https://... your own Robinhood Chain RPC
#   bash walk.sh
#
# It runs a dry run of both orders first and stops if either would fail, asks you to type "send", then:
#   1. TradingAgents: a Buy decision on NVDA, 3 USDG (borro_live.tradingagents)
#   2. ai-hedge-fund: a buy decision on SPY worth 3 USDG (borro_live.ai_hedge_fund)
# and prints every transaction hash and the final positions. CLOSE=1 sells both again at the end.
# PYTHON picks the interpreter (it needs borro-live installed, or eth-account with this folder on the path).
set -euo pipefail
cd "$(dirname "$0")"
PYTHON="${PYTHON:-python3}"
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
: "${BORRO_AGENT_KEY:?set BORRO_AGENT_KEY to the key of the test wallet}"
: "${BORRO_RPC_URL:?set BORRO_RPC_URL to your own Robinhood Chain RPC}"
export BORRO_MAX_ORDER_USDG="${BORRO_MAX_ORDER_USDG:-4}" BORRO_MAX_DAY_USDG="${BORRO_MAX_DAY_USDG:-8}"
unset BORRO_LIVE

echo "== dry run: nothing is sent =="
dry() {
  local out
  out="$("$PYTHON" "$@")" || { echo "$out"; echo "the dry run failed; nothing was sent" >&2; exit 1; }
  echo "$out"
  case "$out" in *"simulated ok"*) ;; *) echo "the dry run did not pass; nothing was sent" >&2; exit 1 ;; esac
}
dry examples/tradingagents_example.py NVDA --decision Buy --usdg 3
dry examples/ai_hedge_fund_example.py --buy SPY 3

echo
echo "The next step sends REAL transactions from the wallet above: two buys of 3 USDG each."
read -r -p 'Type "send" to go on: ' answer
[ "$answer" = "send" ] || { echo "stopped; nothing was sent"; exit 0; }
export BORRO_LIVE=1

echo "== 1. TradingAgents: Buy NVDA, 3 USDG =="
"$PYTHON" examples/tradingagents_example.py NVDA --decision Buy --usdg 3 --live
echo "== 2. ai-hedge-fund: buy SPY, 3 USDG =="
"$PYTHON" examples/ai_hedge_fund_example.py --buy SPY 3 --live

if [ "${CLOSE:-0}" = "1" ]; then
  echo "== closing both =="
  "$PYTHON" -m borro_live sell NVDA --live
  "$PYTHON" -m borro_live sell SPY --live
fi
echo "== final positions =="
"$PYTHON" -m borro_live positions
echo 'Done. To close later: BORRO_LIVE=1 python -m borro_live sell NVDA --live (and SPY).'
