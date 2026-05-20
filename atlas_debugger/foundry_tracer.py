"""Fallback tracer using Foundry (forge test -vvvv) when debug_traceCall is unavailable.

Forge fetches state via eth_call (same as cast call), so it works with any archive RPC.
The -vvvv flag generates traces locally without needing the debug API.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .constants import ChainConfig
from .rpc import get_archive_rpcs
from .tracer import CallFrame, TraceResult


FOUNDRY_TOML = """[profile.default]
src = "src"
out = "out"
libs = ["lib"]
"""

TEST_TEMPLATE = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Test.sol";

contract PDataTrace is Test {{
{arb_mocks}
    function test_trace() public {{
{setup_mocks}
        address simulator = {simulator};
        bytes memory cd = hex"{calldata_hex}";
        (bool ok, bytes memory ret) = simulator.call(cd);
        if (ret.length >= 96) {{
            (bool success, uint8 simResult, uint256 outcome) = abi.decode(ret, (bool, uint8, uint256));
            emit log_named_uint("success", success ? 1 : 0);
            emit log_named_uint("simResult", simResult);
            emit log_named_uint("outcome", outcome);
        }}
    }}
}}
"""

ARB_MOCK_SETUP = """
        // Mock Arbitrum precompiles
        vm.mockCall(
            address(0x6C),
            abi.encodeWithSignature("getPricesInArbGas()"),
            abi.encode(uint256(1e6), uint256(1e6), uint256(1e6), uint256(1e6), uint256(1e6), uint256(1e6))
        );
        vm.mockCall(
            address(0x64),
            abi.encodeWithSignature("arbBlockNumber()"),
            abi.encode(block.number)
        );
"""


def _find_existing_forge_std() -> Path | None:
    """Search for an existing forge-std installation to reuse."""
    search_paths = [
        Path.home() / ".cache" / "atlas-pdata-debugger" / "foundry-project" / "lib" / "forge-std",
    ]

    # Search upward from CWD for any Foundry project
    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        candidate = parent / "lib" / "forge-std" / "src" / "Test.sol"
        if candidate.exists():
            return parent / "lib" / "forge-std"
        if parent == parent.parent:
            break

    # Common development locations
    home = Path.home()
    for pattern_dir in [
        home / "environment",
        home / "projects",
        home / "code",
        home / "src",
    ]:
        if pattern_dir.exists():
            for forge_std in pattern_dir.rglob("lib/forge-std/src/Test.sol"):
                return forge_std.parent.parent

    return None


def _ensure_foundry_project(project_dir: Path) -> bool:
    """Create a minimal Foundry project with forge-std if it doesn't exist."""
    foundry_toml = project_dir / "foundry.toml"
    forge_std = project_dir / "lib" / "forge-std"

    if foundry_toml.exists() and (forge_std.exists() or forge_std.is_symlink()):
        return True

    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "src").mkdir(exist_ok=True)
    (project_dir / "test").mkdir(exist_ok=True)
    (project_dir / "lib").mkdir(exist_ok=True)

    foundry_toml.write_text(FOUNDRY_TOML)

    # Try to symlink an existing forge-std
    existing = _find_existing_forge_std()
    if existing and existing.exists():
        try:
            if not forge_std.exists():
                forge_std.symlink_to(existing)
            return True
        except OSError:
            pass

    # Fall back to forge install
    result = subprocess.run(
        ["forge", "install", "foundry-rs/forge-std", "--no-git", "--no-commit"],
        cwd=str(project_dir),
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode == 0 and forge_std.exists():
        return True

    # Last resort: git clone
    result2 = subprocess.run(
        ["git", "clone", "--depth=1",
         "https://github.com/foundry-rs/forge-std.git",
         str(forge_std)],
        capture_output=True, text=True, timeout=120,
    )
    return forge_std.exists()


def _get_project_dir() -> Path:
    """Get the cached Foundry project directory."""
    cache_dir = Path.home() / ".cache" / "atlas-pdata-debugger" / "foundry-project"
    return cache_dir


def _generate_test(
    calldata: str,
    simulator: str,
    arb_precompiles: bool,
) -> str:
    """Generate a Solidity test file for the pData."""
    cd_hex = calldata
    if cd_hex.startswith("0x"):
        cd_hex = cd_hex[2:]

    arb_mocks = ""
    setup_mocks = ""
    if arb_precompiles:
        setup_mocks = ARB_MOCK_SETUP

    return TEST_TEMPLATE.format(
        simulator=simulator,
        calldata_hex=cd_hex,
        arb_mocks=arb_mocks,
        setup_mocks=setup_mocks,
    )


@dataclass
class ForgeTraceLine:
    depth: int
    call_type: str
    target: str
    value: int
    selector: str
    gas: int
    reverted: bool
    return_data: str = ""


def _parse_forge_trace(output: str) -> CallFrame | None:
    """Parse forge test -vvvv output into a CallFrame tree.

    Forge trace format (each line):
      [depth] call_type target::selector{value: X}(args) [gas]
      [depth] <- [Return|Revert] (data)
    """
    lines = output.split("\n")
    root: CallFrame | None = None
    stack: list[CallFrame] = []

    call_pattern = re.compile(
        r'^\s*\[(\d+)\]\s+'                   # depth
        r'(\w+)\s+'                            # call type (CALL, STATICCALL, etc.)
        r'(0x[0-9a-fA-F]+)'                   # target address
        r'::'                                  # separator
        r'(\w+(?:\([^)]*\))?|[0-9a-f]{8})'   # function name or selector
    )

    revert_pattern = re.compile(
        r'^\s*\[(\d+)\]\s+[←<]-?\s*(Revert|Stop|Return)'
    )

    gas_pattern = re.compile(r'\[(\d+)\s*gas\]')

    for line in lines:
        cm = call_pattern.match(line)
        if cm:
            depth = int(cm.group(1))
            call_type = cm.group(2).upper()
            target = cm.group(3)
            func_name = cm.group(4)

            gm = gas_pattern.search(line)
            gas = int(gm.group(1)) if gm else 0

            frame = CallFrame(
                call_type=call_type,
                from_addr="0x",
                to_addr=target.lower(),
                input_data=f"0x{func_name[:8]}" if not func_name.startswith("0x") else func_name,
                output_data="0x",
                gas=gas,
                gas_used=gas,
                value=0,
                depth=depth,
            )

            while stack and stack[-1].depth >= depth:
                stack.pop()

            if stack:
                stack[-1].calls.append(frame)
            else:
                root = frame

            stack.append(frame)
            continue

        rm = revert_pattern.match(line)
        if rm and stack:
            depth = int(rm.group(1))
            status = rm.group(2)
            while stack and stack[-1].depth > depth:
                stack.pop()
            if stack and status == "Revert":
                stack[-1].error = "execution reverted"
                # Try to extract revert data from the line
                data_match = re.search(r'Revert\s*\(([^)]*)\)', line)
                if data_match:
                    stack[-1].revert_reason = data_match.group(1)
                # Also check for custom error text
                custom_match = re.search(r'custom error\s+([0-9a-f]+):', line)
                if custom_match:
                    stack[-1].output_data = "0x" + custom_match.group(1)

    return root


def forge_trace_at_block(
    calldata: str,
    simulator: str,
    chain: ChainConfig,
    block: int,
    gas_price: int,
    user_rpc: str | None = None,
    verbose: bool = False,
) -> TraceResult:
    """Get a trace using forge test -vvvv with fork mode."""
    project_dir = _get_project_dir()

    if verbose:
        print("  Setting up Foundry project (first time may take a moment)...")

    if not _ensure_foundry_project(project_dir):
        return TraceResult(
            success=False,
            error="Failed to set up Foundry project. Is `forge` installed?",
        )

    # Generate test file
    test_code = _generate_test(calldata, simulator, chain.arb_precompiles)
    test_file = project_dir / "test" / "PDataTrace.t.sol"
    test_file.write_text(test_code)

    rpcs = get_archive_rpcs(chain, user_rpc)
    last_error = ""

    for rpc in rpcs:
        rpc_short = rpc[:50] + "..." if len(rpc) > 50 else rpc
        if verbose:
            print(f"  Trying forge trace via {rpc_short}...")

        try:
            result = subprocess.run(
                [
                    "forge", "test",
                    "--match-test", "test_trace",
                    "--fork-url", rpc,
                    "--fork-block-number", str(block),
                    "--gas-price", str(gas_price),
                    "-vvvv",
                    "--no-match-path", "src/**",
                ],
                cwd=str(project_dir),
                capture_output=True, text=True, timeout=120,
            )

            output = result.stdout + "\n" + result.stderr

            # Check for success markers in forge output
            if "Traces:" in output or "[PASS]" in output or "[FAIL]" in output:
                root = _parse_forge_trace(output)
                if root:
                    return TraceResult(
                        success=True,
                        root_frame=root,
                        rpc_used=rpc,
                    )

                # Even if parsing failed, we have raw output
                return TraceResult(
                    success=True,
                    root_frame=CallFrame(
                        call_type="CALL", from_addr="0x", to_addr=simulator,
                        input_data=calldata[:10], output_data="0x",
                        gas=0, gas_used=0, value=0, depth=0,
                        error="Raw forge output (trace parsing incomplete)",
                        revert_reason=_extract_revert_from_forge(output),
                    ),
                    rpc_used=rpc,
                )

            # Check for archive/RPC errors
            err = output[-500:] if len(output) > 500 else output
            if "missing trie node" in err.lower():
                last_error = f"[{rpc_short}] no archive state"
                continue
            if "error" in err.lower() or result.returncode != 0:
                last_error = f"[{rpc_short}] {err.strip()[-80:]}"
                continue

        except subprocess.TimeoutExpired:
            last_error = f"[{rpc_short}] forge timed out (120s)"
            continue

    return TraceResult(success=False, error=last_error or "All RPCs failed for forge trace")


def _extract_revert_from_forge(output: str) -> str | None:
    """Extract revert reason from forge verbose output."""
    patterns = [
        r'Reason:\s*(.+)',
        r'revert:\s*(.+)',
        r'custom error\s+\w+:\s*(.+)',
        r'Error\("([^"]+)"\)',
        r'StaleReport',
        r'OracleUpdateFailed',
    ]
    for pat in patterns:
        m = re.search(pat, output, re.IGNORECASE)
        if m:
            return m.group(0).strip()
    return None
