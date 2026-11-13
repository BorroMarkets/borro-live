"""borro-live: send a trading agent's decisions to Borro on Robinhood Chain.

    from borro_live import Broker
    broker = Broker.from_env()          # dry run unless BORRO_LIVE=1
    broker.buy("NVDA", 5)               # 5 USDG of NVDA, a plain spot order

Adapters: ``borro_live.tradingagents`` and ``borro_live.ai_hedge_fund``.
"""

from .broker import BorroLiveError, Broker, CapExceeded, Result, SimulationFailed, SpotUnavailable, UnlistedTicker, Unsafe
from .chain import Chain, ChainError
from .mcp import McpClient, McpError, ToolRefused

__version__ = "0.2.0"
__all__ = ["Broker", "Result", "McpClient", "Chain", "BorroLiveError", "UnlistedTicker", "CapExceeded", "SimulationFailed", "SpotUnavailable", "Unsafe",
           "ChainError", "McpError", "ToolRefused"]
