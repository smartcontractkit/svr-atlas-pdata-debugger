"""Locate the on-chain Atlas `metacall` transaction that landed for a given pData.

Strategy
--------
1. The pData comes from an auction for a specific user / userOpHash. The winning
   solver (possibly a different one from the one in pData) eventually broadcasts an
   Atlas `metacall(userOp, solverOps, dAppOp)` transaction.

2. Atlas emits `MetacallResult(address indexed bundler, address indexed user, bool
   solverSuccessful, uint256 ethPaidToBundler, uint256 netGasSurcharge)` on every
   metacall. The `user` topic equals `userOp.from`.

3. We filter `MetacallResult` logs on the Atlas contract (= `userOp.to`) in the
   block range `[oracle_block, deadline]`, then pull each matching transaction's
   input calldata and look for our `userOpHash` hex substring. The tx whose
   calldata contains our userOpHash is the one that landed our pData's auction.

This works regardless of which solver won because we match on `userOpHash`, which
is a commitment to the UserOperation itself (not to any specific solver).
"""

from __future__ import annotations

from dataclasses import dataclass

from .constants import ChainConfig
from .parser import ParsedPData
from .rpc import eth_get_logs, eth_get_transaction_by_hash, find_block_by_timestamp

# keccak256("MetacallResult(address,address,bool,uint256,uint256)")
METACALL_RESULT_TOPIC = "0x1c8af9222013876e762969f616bf76d9bd3a356e39ce598256dd515b6cb7f82b"


@dataclass
class MetacallMatch:
    block_number: int
    tx_hash: str
    log_index: int
    bundler: str  # whoever submitted the metacall (usually the winning solver's bundler)
    user: str  # userOp.from
    solver_successful: bool
    eth_paid_to_bundler: int
    net_gas_surcharge: int


def _pad_addr_topic(addr: str) -> str:
    """Left-pad an address to 32 bytes for use as an indexed topic."""
    addr = addr.lower()
    if addr.startswith("0x"):
        addr = addr[2:]
    return "0x" + addr.rjust(64, "0")


def _decode_metacall_log(log: dict) -> MetacallMatch | None:
    """Decode a MetacallResult log into a MetacallMatch."""
    try:
        topics = log["topics"]
        data = log["data"]
        if data.startswith("0x"):
            data = data[2:]
        bundler = "0x" + topics[1][-40:]
        user = "0x" + topics[2][-40:]
        solver_successful = int(data[:64], 16) != 0
        eth_paid = int(data[64:128], 16)
        net_surcharge = int(data[128:192], 16)
        return MetacallMatch(
            block_number=int(log["blockNumber"], 16),
            tx_hash=log["transactionHash"],
            log_index=int(log["logIndex"], 16),
            bundler=bundler,
            user=user,
            solver_successful=solver_successful,
            eth_paid_to_bundler=eth_paid,
            net_gas_surcharge=net_surcharge,
        )
    except (KeyError, ValueError, IndexError):
        return None


def find_landed_metacall(
    pdata: ParsedPData,
    chain: ChainConfig,
    *,
    user_rpc: str | None = None,
    block_window_after_deadline: int = 5,
    block_window_before_oracle: int = 5,
    verbose: bool = True,
    oracle_block: int | None = None,
) -> tuple[list[MetacallMatch], dict]:
    """Return all MetacallResult matches for this user in the relevant block range,
    and a dict with useful context (oracle_block, deadline, winning match, etc.).

    A match is considered a "landing" if the tx calldata contains our userOpHash.

    `oracle_block` can be passed to avoid redundant RPC lookups when the caller
    has already resolved it.
    """
    ctx: dict = {"candidates": [], "winners": [], "errors": []}

    if not pdata.user_op or not pdata.solver_op:
        ctx["errors"].append("pdata missing user_op or solver_op")
        return [], ctx

    atlas_addr = pdata.user_op.to_addr
    user_from = pdata.user_op.from_addr
    user_op_hash = pdata.solver_op.user_op_hash.lower()
    if user_op_hash.startswith("0x"):
        user_op_hash_no_prefix = user_op_hash[2:]
    else:
        user_op_hash_no_prefix = user_op_hash

    deadline = pdata.user_op.deadline
    if oracle_block is None and pdata.oracle_timestamp:
        oracle_block = find_block_by_timestamp(pdata.oracle_timestamp, chain, user_rpc)

    start_block = (oracle_block - block_window_before_oracle) if oracle_block else (deadline - 300)
    end_block = deadline + block_window_after_deadline

    ctx["atlas"] = atlas_addr
    ctx["user_from"] = user_from
    ctx["user_op_hash"] = user_op_hash
    ctx["oracle_block"] = oracle_block
    ctx["deadline"] = deadline
    ctx["start_block"] = start_block
    ctx["end_block"] = end_block

    if verbose:
        print(f"  Scanning MetacallResult logs on {atlas_addr}")
        print(f"    user (indexed): {user_from}")
        print(f"    block range: {start_block} .. {end_block} ({end_block - start_block + 1} blocks)")

    topics: list[str | None] = [
        METACALL_RESULT_TOPIC,
        None,
        _pad_addr_topic(user_from),
    ]

    ok, logs, err = eth_get_logs(
        address=atlas_addr,
        from_block=start_block,
        to_block=end_block,
        topics=topics,
        chain=chain,
        user_rpc=user_rpc,
    )
    if not ok:
        ctx["errors"].append(f"eth_getLogs failed: {err}")
        if verbose:
            print(f"  eth_getLogs failed: {err}")
        return [], ctx

    if verbose:
        print(f"  Found {len(logs)} MetacallResult events for this user.")

    candidates: list[MetacallMatch] = []
    winners: list[MetacallMatch] = []
    for log in logs:
        match = _decode_metacall_log(log)
        if not match:
            continue
        candidates.append(match)

        ok_tx, tx, tx_err = eth_get_transaction_by_hash(match.tx_hash, chain, user_rpc)
        if not ok_tx or tx is None:
            ctx["errors"].append(f"tx fetch failed for {match.tx_hash}: {tx_err}")
            continue
        tx_input = (tx.get("input") or "").lower()
        if user_op_hash_no_prefix in tx_input:
            winners.append(match)

    ctx["candidates"] = candidates
    ctx["winners"] = winners
    return winners, ctx
