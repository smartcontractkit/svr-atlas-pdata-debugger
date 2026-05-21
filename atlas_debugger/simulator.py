"""Simulate pData via eth_call, with multi-RPC failover."""

from __future__ import annotations

from dataclasses import dataclass

from .constants import (
    RESULT_NAMES,
    SOLVER_OUTCOME_BITS,
    VERIFICATION_FAIL_CODES,
    ChainConfig,
)
from .rpc import RPCResult, cast_call_multi, get_archive_rpcs


@dataclass
class SimResult:
    block: int
    success: bool
    result_code: int
    result_name: str
    outcome: int
    outcome_bits: list[str]
    rpc_used: str = ""
    raw_hex: str | None = None
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.success and self.result_code == 6

    def summary(self) -> str:
        if self.error:
            return f"Block {self.block}: RPC ERROR - {self.error}"
        status = "PASS" if self.passed else "FAIL"
        bits_str = ", ".join(self.outcome_bits) if self.outcome_bits else "none"
        extra = ""
        if self.result_code == 1 and self.outcome in VERIFICATION_FAIL_CODES:
            extra = f" ({VERIFICATION_FAIL_CODES[self.outcome]})"
        return (
            f"Block {self.block}: {self.result_name} [{status}] "
            f"outcome={self.outcome}{extra} bits=[{bits_str}]"
        )


def _decode_outcome_bits(outcome: int) -> list[str]:
    bits = []
    for bit, name in SOLVER_OUTCOME_BITS.items():
        if outcome & (1 << bit):
            bits.append(name)
    return bits


def simulate_at_block(
    calldata: str,
    simulator: str,
    chain: ChainConfig,
    block: int,
    gas_price: int,
    user_rpc: str | None = None,
    verbose: bool = False,
) -> SimResult:
    """Run eth_call via cast call, auto-trying multiple archive RPCs."""
    rpcs = get_archive_rpcs(chain, user_rpc)

    cast_args = [
        "cast", "call", simulator, calldata,
        "--block", str(block),
        "--gas-price", str(gas_price),
    ]

    result = cast_call_multi(
        args=cast_args,
        rpcs=rpcs,
        retries_per_rpc=3,
        timeout=30,
        verbose=verbose,
    )

    if not result.success:
        return SimResult(
            block=block, success=False, result_code=-1,
            result_name="RPCError", outcome=0, outcome_bits=[],
            error=result.error,
        )

    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if line.startswith("0x") and len(line) >= 194:
            success_val = int(line[2:66], 16)
            result_code = int(line[66:130], 16)
            outcome = int(line[130:194], 16)
            return SimResult(
                block=block,
                success=bool(success_val),
                result_code=result_code,
                result_name=RESULT_NAMES.get(result_code, f"Unknown({result_code})"),
                outcome=outcome,
                outcome_bits=_decode_outcome_bits(outcome),
                rpc_used=result.rpc_used,
                raw_hex=line,
            )

    return SimResult(
        block=block, success=False, result_code=-1,
        result_name="ParseError", outcome=0, outcome_bits=[],
        error=f"Could not parse response: {result.stdout.strip()[:80]}",
    )


def get_current_block(chain: ChainConfig, user_rpc: str | None = None) -> int:
    """Get current block number, trying multiple RPCs."""
    rpcs = get_archive_rpcs(chain, user_rpc)

    result = cast_call_multi(
        args=["cast", "bn"],
        rpcs=rpcs,
        retries_per_rpc=2,
        timeout=15,
    )

    if result.success:
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line.isdigit():
                return int(line)

    raise RuntimeError(f"Failed to get block number from any RPC: {result.error}")
