"""Fetch execution traces via debug_traceCall with multi-RPC failover."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .constants import ChainConfig
from .rpc import curl_post_multi, get_archive_rpcs


@dataclass
class CallFrame:
    """A single frame in the call trace tree."""
    call_type: str  # CALL, STATICCALL, DELEGATECALL, CREATE, etc.
    from_addr: str
    to_addr: str
    input_data: str
    output_data: str
    gas: int
    gas_used: int
    value: int
    error: str | None = None
    revert_reason: str | None = None
    depth: int = 0
    calls: list["CallFrame"] = field(default_factory=list)

    @property
    def selector(self) -> str:
        if len(self.input_data) >= 10:
            return self.input_data[:10]
        return self.input_data

    @property
    def reverted(self) -> bool:
        return self.error is not None and self.error != ""


@dataclass
class TraceResult:
    """Result of a debug_traceCall."""
    success: bool
    root_frame: CallFrame | None = None
    raw_json_path: str | None = None
    rpc_used: str = ""
    error: str | None = None

    @property
    def has_trace(self) -> bool:
        return self.root_frame is not None


def _parse_call_frame(obj: dict, depth: int = 0) -> CallFrame:
    """Recursively parse a callTracer JSON object into CallFrame tree."""
    frame = CallFrame(
        call_type=obj.get("type", "CALL"),
        from_addr=obj.get("from", "0x"),
        to_addr=obj.get("to", "0x"),
        input_data=obj.get("input", "0x"),
        output_data=obj.get("output", "0x"),
        gas=int(obj.get("gas", "0x0"), 16) if isinstance(obj.get("gas"), str) else obj.get("gas", 0),
        gas_used=int(obj.get("gasUsed", "0x0"), 16) if isinstance(obj.get("gasUsed"), str) else obj.get("gasUsed", 0),
        value=int(obj.get("value", "0x0"), 16) if isinstance(obj.get("value"), str) else obj.get("value", 0),
        error=obj.get("error"),
        revert_reason=obj.get("revertReason"),
        depth=depth,
    )
    for sub in obj.get("calls", []):
        frame.calls.append(_parse_call_frame(sub, depth + 1))
    return frame


def trace_at_block(
    calldata: str,
    simulator: str,
    chain: ChainConfig,
    block: int,
    gas_price: int,
    user_rpc: str | None = None,
    output_file: str | None = None,
    verbose: bool = False,
) -> TraceResult:
    """Execute debug_traceCall, auto-trying multiple archive RPCs."""
    rpcs = get_archive_rpcs(chain, user_rpc)
    block_hex = hex(block)

    payload = {
        "jsonrpc": "2.0",
        "method": "debug_traceCall",
        "params": [
            {
                "to": simulator,
                "data": calldata if calldata.startswith("0x") else "0x" + calldata,
                "gasPrice": hex(gas_price),
            },
            block_hex,
            {"tracer": "callTracer", "tracerConfig": {"withLog": True}},
        ],
        "id": 1,
    }

    success, trace_data, rpc_used, error = curl_post_multi(
        payload=payload,
        rpcs=rpcs,
        retries_per_rpc=3,
        timeout=90,
        verbose=verbose,
    )

    if not success or not isinstance(trace_data, dict):
        return TraceResult(success=False, error=error or "No trace data returned")

    if output_file:
        with open(output_file, "w") as f:
            json.dump(trace_data, f, indent=2)

    root = _parse_call_frame(trace_data)
    return TraceResult(
        success=True,
        root_frame=root,
        raw_json_path=output_file,
        rpc_used=rpc_used,
    )


def find_all_reverts(frame: CallFrame) -> list[CallFrame]:
    """Walk the call tree and collect all frames that reverted."""
    reverts: list[CallFrame] = []
    if frame.reverted:
        reverts.append(frame)
    for child in frame.calls:
        reverts.extend(find_all_reverts(child))
    return reverts


def find_deepest_revert(frame: CallFrame) -> CallFrame | None:
    """Find the deepest (root cause) revert in the call tree."""
    reverts = find_all_reverts(frame)
    if not reverts:
        return None
    return max(reverts, key=lambda f: f.depth)


def print_call_tree(frame: CallFrame, max_depth: int = 10, _current_depth: int = 0) -> None:
    """Pretty-print the call tree to stdout."""
    if _current_depth > max_depth:
        return

    indent = "  " * (_current_depth + 1)
    addr = frame.to_addr
    sel = frame.selector if frame.selector != "0x" else "(fallback)"
    err_mark = " REVERT" if frame.reverted else ""
    value_str = f" value={frame.value}" if frame.value > 0 else ""

    print(f"{indent}{frame.call_type} {addr}::{sel}{value_str} gas={frame.gas_used}{err_mark}")

    if frame.reverted and frame.error:
        print(f"{indent}  error: {frame.error}")
    if frame.revert_reason:
        print(f"{indent}  revertReason: {frame.revert_reason}")

    for child in frame.calls:
        print_call_tree(child, max_depth, _current_depth + 1)
