"""The chain side: reads through the user's own RPC, and signing and sending with the agent's own key."""

from __future__ import annotations

import time

from eth_abi import decode, encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

from .mcp import McpError, http_post

CHAIN_ID = 4663  # Robinhood Chain


class ChainError(Exception):
    """The RPC refused a call, or a transaction did not go through."""


class Reverted(ChainError):
    """A call or a gas estimate reverted: the transaction would fail, so it is not sent."""


def selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def calldata(signature: str, types: list[str], values: list) -> str:
    return "0x" + (selector(signature) + encode(types, values)).hex()


class Chain:
    """JSON-RPC to the user's own Robinhood Chain endpoint. The address is kept private and never printed."""

    def __init__(self, rpc_url: str, timeout: float = 60, transport=None):
        self.__url = rpc_url
        self.timeout = timeout
        self._transport = transport or (lambda payload: http_post(self.__url, payload, self.timeout, "the RPC"))
        self._id = 0
        self._checked = False

    def __repr__(self) -> str:
        return "Chain(<your RPC>)"

    def rpc(self, method: str, params: list | None = None):
        self._id += 1
        try:
            res = self._transport({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or []})
        except McpError as e:
            raise ChainError(str(e)) from None
        if "error" in res:
            msg = str((res["error"] or {}).get("message", "error"))[:300]
            raise (Reverted if "revert" in msg.lower() else ChainError)(f"{method}: {msg}")
        return res.get("result")

    def check_chain(self) -> None:
        if not self._checked:
            got = int(self.rpc("eth_chainId"), 16)
            if got != CHAIN_ID:
                raise ChainError(f"the RPC is on chain {got}, not Robinhood Chain ({CHAIN_ID})")
            self._checked = True

    def call(self, to: str, data: str, sender: str | None = None, value: int = 0) -> bytes:
        tx = {"to": to, "data": data}
        if sender:
            tx["from"] = sender
        if value:
            tx["value"] = hex(value)
        return bytes.fromhex(self.rpc("eth_call", [tx, "latest"])[2:])

    def read(self, to: str, signature: str, types: list[str], values: list, out: list[str]):
        return decode(out, self.call(to, calldata(signature, types, values)))

    def simulate(self, sender: str, txs: list[dict]) -> list[dict] | None:
        """Every step in order from the sender (eth_simulateV1). None when the RPC does not offer it."""
        calls = [{"from": sender, "to": t["to"], "data": t["data"], **({"value": hex(int(t["value"]))} if int(t.get("value", 0)) else {})} for t in txs]
        try:
            out = self.rpc("eth_simulateV1", [{"blockStateCalls": [{"calls": calls}], "validation": False}, "latest"])
        except ChainError:
            return None
        steps = []
        for i, c in enumerate(out[0]["calls"]):
            ok = c.get("status") == "0x1"
            steps.append({"step": i + 1, "ok": ok, **({} if ok else {"error": str((c.get("error") or {}).get("message", "reverted"))[:200]})})
        return steps

    def send(self, account, tx: dict, wait_seconds: float = 180) -> dict:
        """Sign one transaction with the agent's key, send it, wait for its receipt and check it went through.

        The call is run from the wallet first (eth_call, then the gas estimate): a transaction that would revert is
        never sent. Returns {hash, status, gas_used, block}.
        """
        self.check_chain()
        me = account.address
        to, data, value = to_checksum_address(tx["to"]), tx["data"], int(tx.get("value", 0))
        self.call(to, data, sender=me, value=value)
        req = {"from": me, "to": to, "data": data, **({"value": hex(value)} if value else {})}
        gas = int(self.rpc("eth_estimateGas", [req]), 16) * 13 // 10
        base = int((self.rpc("eth_getBlockByNumber", ["latest", False]) or {}).get("baseFeePerGas", "0x0"), 16)
        try:
            tip = int(self.rpc("eth_maxPriorityFeePerGas"), 16)
        except ChainError:
            tip = 0
        signed = account.sign_transaction({
            "type": 2, "chainId": CHAIN_ID, "to": to, "data": data, "value": value, "gas": gas,
            "nonce": int(self.rpc("eth_getTransactionCount", [me, "pending"]), 16),
            "maxFeePerGas": base * 2 + tip + 1, "maxPriorityFeePerGas": tip,
        })
        tx_hash = self.rpc("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()])
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            rc = self.rpc("eth_getTransactionReceipt", [tx_hash])
            if rc:
                out = {"hash": tx_hash, "status": int(rc["status"], 16), "gas_used": int(rc["gasUsed"], 16), "block": int(rc["blockNumber"], 16)}
                if out["status"] != 1:
                    raise ChainError(f"transaction {tx_hash} was mined and failed")
                return out
            time.sleep(0.5)
        raise ChainError(f"transaction {tx_hash} was sent and has no receipt after {int(wait_seconds)} seconds; check it before sending anything else")


def account_from_key(key: str):
    """The agent's signer. The key lives only inside this object: nothing here prints, logs or stores it."""
    try:
        return Account.from_key(key.strip())
    except Exception:
        raise ValueError("the agent key is not a private key (0x and 64 hex characters)") from None
