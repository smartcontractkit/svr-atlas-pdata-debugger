"""Multi-RPC provider with automatic failover and aggressive retry logic.

Ships with built-in free archive RPCs so users don't need their own archive node.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass

from .constants import ChainConfig

# Free archive-capable RPCs per chain, ordered by reliability.
# drpc.org supports archive on all chains. Others are chain-specific.
ARCHIVE_RPCS: dict[int, list[str]] = {
    42161: [  # Arbitrum
        "https://arbitrum.drpc.org",
        "https://arb-mainnet.g.alchemy.com/v2/demo",
        "https://rpc.ankr.com/arbitrum",
        "https://arb1.arbitrum.io/rpc",
    ],
    56: [  # BSC
        "https://bsc.drpc.org",
        "https://rpc.ankr.com/bsc",
        "https://bsc-dataseed.binance.org",
    ],
    8453: [  # Base
        "https://base.drpc.org",
        "https://rpc.ankr.com/base",
        "https://mainnet.base.org",
    ],
    1: [  # Ethereum
        "https://eth.drpc.org",
        "https://rpc.ankr.com/eth",
        "https://ethereum-rpc.publicnode.com",
    ],
}


@dataclass
class RPCResult:
    """Result of a single RPC call."""
    success: bool
    stdout: str = ""
    stderr: str = ""
    rpc_used: str = ""
    error: str | None = None


def get_archive_rpcs(chain: ChainConfig, user_rpc: str | None = None) -> list[str]:
    """Get list of RPCs to try, user-provided first, then built-in archive RPCs."""
    rpcs: list[str] = []
    if user_rpc:
        rpcs.append(user_rpc)
    built_in = ARCHIVE_RPCS.get(chain.chain_id, [])
    for r in built_in:
        if r not in rpcs:
            rpcs.append(r)
    for r in chain.rpcs:
        if r not in rpcs:
            rpcs.append(r)
    return rpcs


def _is_retryable_error(err: str) -> bool:
    """Check if an error is transient and worth retrying."""
    retryable_patterns = [
        "incorrect response",
        "temporary",
        "rate limit",
        "429",
        "500",
        "502",
        "503",
        "504",
        "tls handshake",
        "connection reset",
        "connection refused",
        "timeout",
        "eof",
        "broken pipe",
        "no such host",
    ]
    err_lower = err.lower()
    return any(p in err_lower for p in retryable_patterns)


def _is_archive_error(err: str) -> bool:
    """Check if error indicates the node lacks archive state."""
    return any(p in err.lower() for p in [
        "missing trie node",
        "required historical state",
        "state not available",
        "block not found",
        "header not found",
    ])


def find_block_by_timestamp(
    timestamp: int,
    chain: ChainConfig,
    user_rpc: str | None = None,
) -> int | None:
    """Convert a Unix timestamp to a block number using cast find-block."""
    rpcs = get_archive_rpcs(chain, user_rpc)
    result = cast_call_multi(
        args=["cast", "find-block", str(timestamp)],
        rpcs=rpcs,
        retries_per_rpc=2,
        timeout=30,
    )
    if result.success:
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line.isdigit():
                return int(line)
    return None


def eth_get_logs(
    address: str,
    from_block: int,
    to_block: int,
    topics: list[str | None] | None,
    chain: ChainConfig,
    user_rpc: str | None = None,
) -> tuple[bool, list[dict], str]:
    """JSON-RPC eth_getLogs across the configured RPCs with failover.

    Returns (success, logs_list, error_message).
    `topics` entries can be None to skip a slot (null in JSON-RPC).
    """
    rpcs = get_archive_rpcs(chain, user_rpc)
    topics_encoded: list[str | None] = list(topics) if topics else []
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_getLogs",
        "params": [
            {
                "address": address,
                "fromBlock": hex(from_block),
                "toBlock": hex(to_block),
                "topics": topics_encoded,
            }
        ],
    }
    ok, result, _rpc_used, err = curl_post_multi(payload, rpcs, retries_per_rpc=2, timeout=60)
    if ok and isinstance(result, list):
        return True, result, ""
    return False, [], err


def eth_get_transaction_by_hash(
    tx_hash: str,
    chain: ChainConfig,
    user_rpc: str | None = None,
) -> tuple[bool, dict | None, str]:
    """JSON-RPC eth_getTransactionByHash."""
    rpcs = get_archive_rpcs(chain, user_rpc)
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionByHash", "params": [tx_hash]}
    ok, result, _rpc_used, err = curl_post_multi(payload, rpcs, retries_per_rpc=2, timeout=30)
    if ok and isinstance(result, dict):
        return True, result, ""
    return False, None, err


def describe_feed(
    feed_address: str,
    chain: ChainConfig,
    user_rpc: str | None = None,
) -> dict:
    """Best-effort lookup of a Chainlink feed's description() and decimals() via RPC.

    Returns a dict like {"description": "ETH / USD", "decimals": 8} or an empty dict
    if no RPC worked.
    """
    rpcs = get_archive_rpcs(chain, user_rpc)
    info: dict = {}

    desc_res = cast_call_multi(
        args=["cast", "call", feed_address, "description()(string)"],
        rpcs=rpcs,
        retries_per_rpc=2,
        timeout=15,
    )
    if desc_res.success:
        line = desc_res.stdout.strip().split("\n")[0].strip().strip('"')
        if line:
            info["description"] = line

    dec_res = cast_call_multi(
        args=["cast", "call", feed_address, "decimals()(uint8)"],
        rpcs=rpcs,
        retries_per_rpc=2,
        timeout=15,
    )
    if dec_res.success:
        line = dec_res.stdout.strip().split("\n")[0].strip()
        if line.isdigit():
            info["decimals"] = int(line)

    return info


def cast_call_multi(
    args: list[str],
    rpcs: list[str],
    retries_per_rpc: int = 3,
    timeout: int = 30,
    verbose: bool = False,
) -> RPCResult:
    """Run a cast command, automatically trying multiple RPCs with retries.

    Args:
        args: cast command args WITHOUT --rpc-url (e.g. ["cast", "call", addr, data, "--block", "123"])
        rpcs: list of RPC URLs to try in order
        retries_per_rpc: retries per individual RPC
        timeout: seconds per attempt
        verbose: print progress to stderr
    """
    all_errors: list[str] = []

    for rpc_idx, rpc in enumerate(rpcs):
        rpc_short = rpc[:50] + "..." if len(rpc) > 50 else rpc

        for attempt in range(retries_per_rpc):
            try:
                cmd = args + ["--rpc-url", rpc]
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=timeout,
                )

                if result.returncode == 0 and result.stdout.strip():
                    return RPCResult(
                        success=True,
                        stdout=result.stdout,
                        stderr=result.stderr,
                        rpc_used=rpc,
                    )

                err = result.stderr.strip()
                if not err:
                    err = result.stdout.strip() or "empty response"
                last_line = err.split("\n")[-1]

                if _is_archive_error(last_line):
                    if verbose:
                        print(f"  [{rpc_short}] No archive state, trying next RPC...", file=sys.stderr)
                    all_errors.append(f"[{rpc_short}] {last_line[:80]}")
                    break  # skip retries, this RPC won't have archive

                if _is_retryable_error(last_line):
                    all_errors.append(f"[{rpc_short}] attempt {attempt+1}: {last_line[:80]}")
                    if attempt < retries_per_rpc - 1:
                        wait = 2 + attempt * 3
                        if verbose:
                            print(f"  [{rpc_short}] Retrying in {wait}s... ({last_line[:40]})", file=sys.stderr)
                        time.sleep(wait)
                        continue
                    break  # exhausted retries for this RPC

                # Non-retryable error
                all_errors.append(f"[{rpc_short}] {last_line[:80]}")
                break

            except subprocess.TimeoutExpired:
                all_errors.append(f"[{rpc_short}] attempt {attempt+1}: timeout ({timeout}s)")
                if attempt < retries_per_rpc - 1:
                    time.sleep(2)
                    continue
                break

    return RPCResult(
        success=False,
        error=" | ".join(all_errors[-3:]) if all_errors else "All RPCs failed",
    )


def curl_post_multi(
    payload: dict,
    rpcs: list[str],
    retries_per_rpc: int = 3,
    timeout: int = 90,
    verbose: bool = False,
) -> tuple[bool, dict | None, str, str]:
    """Send a JSON-RPC POST request, trying multiple RPCs.

    Returns: (success, parsed_result_or_none, rpc_used, error_msg)
    """
    payload_json = json.dumps(payload)
    all_errors: list[str] = []

    for rpc_idx, rpc in enumerate(rpcs):
        rpc_short = rpc[:50] + "..." if len(rpc) > 50 else rpc

        for attempt in range(retries_per_rpc):
            try:
                result = subprocess.run(
                    [
                        "curl", "-s", "-X", "POST",
                        "-H", "Content-Type: application/json",
                        "-d", payload_json,
                        "--max-time", str(timeout),
                        rpc,
                    ],
                    capture_output=True, text=True, timeout=timeout + 30,
                )

                if result.returncode != 0:
                    err = result.stderr.strip()[:80] or "curl non-zero exit"
                    all_errors.append(f"[{rpc_short}] {err}")
                    if _is_retryable_error(err) and attempt < retries_per_rpc - 1:
                        time.sleep(3 + attempt * 3)
                        continue
                    break

                body = result.stdout.strip()
                if not body:
                    all_errors.append(f"[{rpc_short}] empty response")
                    if attempt < retries_per_rpc - 1:
                        time.sleep(3 + attempt * 3)
                        continue
                    break

                resp = json.loads(body)

                if "error" in resp:
                    err_msg = resp["error"]
                    if isinstance(err_msg, dict):
                        err_msg = err_msg.get("message", str(err_msg))
                    err_str = str(err_msg)

                    if _is_archive_error(err_str):
                        all_errors.append(f"[{rpc_short}] no archive: {err_str[:60]}")
                        break

                    if "debug" in err_str.lower() and "not" in err_str.lower():
                        all_errors.append(f"[{rpc_short}] debug API not supported: {err_str[:60]}")
                        break

                    if _is_retryable_error(err_str) and attempt < retries_per_rpc - 1:
                        all_errors.append(f"[{rpc_short}] attempt {attempt+1}: {err_str[:60]}")
                        time.sleep(3 + attempt * 3)
                        continue

                    all_errors.append(f"[{rpc_short}] {err_str[:80]}")
                    break

                trace_data = resp.get("result")
                if trace_data is not None:
                    return True, trace_data, rpc, ""

                all_errors.append(f"[{rpc_short}] no 'result' in response")
                break

            except json.JSONDecodeError:
                all_errors.append(f"[{rpc_short}] invalid JSON response")
                if attempt < retries_per_rpc - 1:
                    time.sleep(3 + attempt * 3)
                    continue
                break

            except subprocess.TimeoutExpired:
                all_errors.append(f"[{rpc_short}] timeout ({timeout}s)")
                if attempt < retries_per_rpc - 1:
                    time.sleep(3)
                    continue
                break

    combined_err = " | ".join(all_errors[-3:]) if all_errors else "All RPCs failed"
    return False, None, "", combined_err
