"""Analyze execution traces to produce human-readable failure diagnoses."""

from __future__ import annotations

from dataclasses import dataclass, field

from .constants import KNOWN_ERROR_SELECTORS
from .tracer import CallFrame, TraceResult, find_all_reverts, find_deepest_revert


@dataclass
class Diagnosis:
    """Structured diagnosis from trace analysis."""
    severity: str  # "root_cause", "contributing", "info"
    title: str
    detail: str
    frame: CallFrame | None = None
    suggestions: list[str] = field(default_factory=list)


# Well-known contract labels
KNOWN_CONTRACTS: dict[str, str] = {
    "0x82af49447d8a07e3bd95bd0d56f35241523fbab1": "WETH (Arbitrum)",
    "0x4200000000000000000000000000000000000006": "WETH (Base)",
    "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c": "WBNB (BSC)",
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": "WETH (Ethereum)",
}

# Chainlink-related known patterns
CHAINLINK_SELECTORS = {"0xfeaf968c", "0x50d25bcd", "0x9a6fc8f5"}


def _decode_error_string(output_hex: str) -> str | None:
    """Try to decode an Error(string) revert reason from output data."""
    data = output_hex
    if data.startswith("0x"):
        data = data[2:]

    if data.startswith("08c379a0") and len(data) >= 136:
        try:
            str_offset = int(data[8:72], 16)
            str_start = 8 + str_offset * 2
            str_len = int(data[str_start : str_start + 64], 16)
            hex_str = data[str_start + 64 : str_start + 64 + str_len * 2]
            return bytes.fromhex(hex_str).decode("utf-8", errors="replace")
        except (ValueError, IndexError):
            return None
    return None


def _decode_panic_code(output_hex: str) -> str | None:
    """Decode Panic(uint256) code."""
    data = output_hex
    if data.startswith("0x"):
        data = data[2:]

    if data.startswith("4e487b71") and len(data) >= 72:
        try:
            code = int(data[8:72], 16)
            panic_codes = {
                0x00: "generic compiler panic",
                0x01: "assert failed",
                0x11: "arithmetic overflow/underflow",
                0x12: "division by zero",
                0x21: "enum conversion error",
                0x22: "storage encoding error",
                0x31: "pop on empty array",
                0x32: "array out of bounds",
                0x41: "too much memory allocated",
                0x51: "zero-initialized function pointer",
            }
            return panic_codes.get(code, f"unknown panic code {code:#x}")
        except (ValueError, IndexError):
            return None
    return None


def _identify_known_error(output_hex: str) -> str | None:
    """Check if the output matches any known error selector."""
    data = output_hex
    if data.startswith("0x"):
        data = data[2:]

    if len(data) >= 8:
        sel = data[:8]
        if sel in KNOWN_ERROR_SELECTORS:
            name = KNOWN_ERROR_SELECTORS[sel]
            if name == "Error(string)":
                decoded = _decode_error_string(output_hex)
                if decoded:
                    return f'Error("{decoded}")'
                return "Error(string) - could not decode"
            if name == "Panic(uint256)":
                decoded = _decode_panic_code(output_hex)
                if decoded:
                    return f"Panic: {decoded}"
                return "Panic(uint256) - could not decode"
            return name
    return None


def _get_contract_label(addr: str) -> str:
    """Return a human-friendly label for known contracts."""
    return KNOWN_CONTRACTS.get(addr.lower(), addr)


def _is_chainlink_call(frame: CallFrame) -> bool:
    """Heuristic: is this frame a Chainlink oracle call?"""
    return frame.selector in CHAINLINK_SELECTORS


def analyze_trace(trace: TraceResult, solver_addr: str | None = None) -> list[Diagnosis]:
    """Analyze a trace and produce a list of diagnoses, ordered by severity."""
    if not trace.has_trace or not trace.root_frame:
        return [Diagnosis(
            severity="root_cause",
            title="No trace available",
            detail="The trace could not be obtained. Check RPC connectivity.",
            suggestions=["Retry with a different archive RPC that supports debug_traceCall"],
        )]

    diagnoses: list[Diagnosis] = []
    root = trace.root_frame

    all_reverts = find_all_reverts(root)
    deepest = find_deepest_revert(root)

    if not all_reverts:
        diagnoses.append(Diagnosis(
            severity="info",
            title="No reverts found in trace",
            detail="The call tree completed without any REVERT. "
                   "The simulation result may come from return value logic, not a revert.",
        ))
        return diagnoses

    # Analyze the deepest revert (most likely root cause)
    if deepest:
        error_name = None
        error_detail = ""

        # Check output data for known error
        if deepest.output_data and deepest.output_data != "0x":
            error_name = _identify_known_error(deepest.output_data)

        # Check the error field from tracer
        if not error_name and deepest.error:
            error_name = deepest.error

        # Check revertReason field
        if not error_name and deepest.revert_reason:
            error_name = deepest.revert_reason

        target_label = _get_contract_label(deepest.to_addr)
        sel = deepest.selector

        # Build specific diagnoses based on pattern matching
        if error_name and "StaleReport" in error_name:
            diagnoses.append(Diagnosis(
                severity="root_cause",
                title="Oracle StaleReport",
                detail=f"The Chainlink oracle report is stale at {target_label}. "
                       f"The oracle price data was too old for the DApp's freshness requirement.",
                frame=deepest,
                suggestions=[
                    "This is a timing issue - the oracle hadn't updated yet at this block.",
                    "Use `sweep` to find the exact block where StaleReport starts.",
                    "The searcher cannot fix this; it depends on Chainlink update frequency.",
                ],
            ))

        elif error_name and "OracleUpdateFailed" in error_name:
            diagnoses.append(Diagnosis(
                severity="root_cause",
                title="Oracle Update Failed",
                detail=f"The oracle update call failed at {target_label}.",
                frame=deepest,
                suggestions=[
                    "The oracle report embedded in the UserOp may be malformed or expired.",
                    "Check if the report data in UserOp.data is valid.",
                ],
            ))

        elif error_name and "burn amount exceeds balance" in str(error_name):
            diagnoses.append(Diagnosis(
                severity="root_cause",
                title="Insufficient WETH/Token Balance",
                detail=f"Call to {target_label} reverted with '{error_name}'. "
                       f"The solver contract does not hold enough tokens to complete the withdrawal/transfer.",
                frame=deepest,
                suggestions=[
                    "Check solver contract's token balance at this block.",
                    "The solver likely attempted a liquidation that didn't yield enough tokens.",
                    "Verify the liquidation/swap logic produces sufficient balance before WETH.withdraw().",
                ],
            ))

        elif error_name and "transfer amount exceeds balance" in str(error_name):
            diagnoses.append(Diagnosis(
                severity="root_cause",
                title="Insufficient Token Balance for Transfer",
                detail=f"ERC20 transfer at {target_label} failed: '{error_name}'. "
                       f"The sender does not have enough tokens.",
                frame=deepest,
                suggestions=[
                    "Check the solver contract's token balance at this block.",
                    "The preceding swap/liquidation may have failed silently or returned fewer tokens.",
                ],
            ))

        elif error_name and ("overflow" in str(error_name).lower() or "underflow" in str(error_name).lower()):
            diagnoses.append(Diagnosis(
                severity="root_cause",
                title="Arithmetic Overflow/Underflow",
                detail=f"Panic at {target_label}: {error_name}. "
                       f"An arithmetic operation exceeded safe bounds.",
                frame=deepest,
                suggestions=[
                    "Check input parameters for extreme values.",
                    "This may indicate a calculation bug in the solver or target protocol.",
                ],
            ))

        else:
            # Generic revert
            detail = f"Deepest revert at {target_label}::{sel} (depth={deepest.depth})"
            if error_name:
                detail += f"\n  Error: {error_name}"
            if deepest.output_data and deepest.output_data != "0x" and len(deepest.output_data) > 2:
                detail += f"\n  Output: {deepest.output_data[:80]}{'...' if len(deepest.output_data) > 80 else ''}"

            diagnoses.append(Diagnosis(
                severity="root_cause",
                title=f"Revert in {target_label}",
                detail=detail,
                frame=deepest,
                suggestions=[
                    f"Decode the selector {sel} to identify the function that reverted.",
                    "Check the output data for a custom error or Error(string).",
                    f"Use `cast 4byte {sel}` to look up the function signature.",
                ],
            ))

    # Check for solver-specific reverts
    if solver_addr:
        solver_lower = solver_addr.lower()
        for rev in all_reverts:
            if rev.to_addr.lower() == solver_lower and rev != deepest:
                error_name = None
                if rev.output_data and rev.output_data != "0x":
                    error_name = _identify_known_error(rev.output_data)
                diagnoses.append(Diagnosis(
                    severity="contributing",
                    title=f"Solver contract reverted",
                    detail=f"Revert in solver {rev.to_addr}::{rev.selector} "
                           f"(depth={rev.depth})"
                           + (f"\n  Error: {error_name}" if error_name else ""),
                    frame=rev,
                ))

    # Summary of all revert locations
    if len(all_reverts) > 1:
        locations = []
        for r in all_reverts:
            label = _get_contract_label(r.to_addr)
            locations.append(f"  depth={r.depth}: {r.call_type} {label}::{r.selector}")
        diagnoses.append(Diagnosis(
            severity="info",
            title=f"Revert chain ({len(all_reverts)} frames)",
            detail="All reverted frames in the call tree:\n" + "\n".join(locations),
        ))

    return diagnoses


def format_diagnoses(diagnoses: list[Diagnosis]) -> str:
    """Format diagnoses into a human-readable string."""
    lines: list[str] = []

    severity_icon = {
        "root_cause": "[ROOT CAUSE]",
        "contributing": "[CONTRIBUTING]",
        "info": "[INFO]",
    }

    for i, d in enumerate(diagnoses, 1):
        icon = severity_icon.get(d.severity, "[?]")
        lines.append(f"  {icon} {d.title}")
        lines.append("")
        for detail_line in d.detail.split("\n"):
            lines.append(f"    {detail_line}")
        lines.append("")

        if d.frame:
            lines.append(f"    Location: {d.frame.call_type} {d.frame.to_addr}::{d.frame.selector}")
            lines.append(f"    Depth: {d.frame.depth}, Gas used: {d.frame.gas_used}")
            if d.frame.error:
                lines.append(f"    Error field: {d.frame.error}")
            lines.append("")

        if d.suggestions:
            lines.append("    Suggestions:")
            for s in d.suggestions:
                lines.append(f"      - {s}")
            lines.append("")

    return "\n".join(lines)
