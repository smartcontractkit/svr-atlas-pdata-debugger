"""CLI entry point for atlas-pdata-debugger."""

from __future__ import annotations

import argparse
import datetime
import os
import re
import subprocess
import sys
import time

from .analyzer import analyze_trace, format_diagnoses
from .chain import detect_chain
from .constants import RESULT_NAMES, VERIFICATION_FAIL_CODES
from .find_tx import find_landed_metacall
from .forge_gen import write_test
from .foundry_tracer import forge_trace_at_block
from .parser import parse_pdata
from .rpc import describe_feed, find_block_by_timestamp, get_archive_rpcs
from .simulator import get_current_block, simulate_at_block
from .tracer import find_deepest_revert, print_call_tree, trace_at_block


def _print_header(title: str) -> None:
    width = 60
    print("=" * width)
    print(f"  {title}")
    print("=" * width)


def _print_field(label: str, value: str, indent: int = 2) -> None:
    print(f"{' ' * indent}{label:<22} {value}")


def _format_block_targets(start_block: int, end_block: int, preview: int = 40) -> str:
    """Format target blocks compactly for console output."""
    if end_block < start_block:
        return "(none)"
    total = end_block - start_block + 1
    if total <= preview:
        return ", ".join(str(b) for b in range(start_block, end_block + 1))

    head_n = 8
    tail_n = 8
    head = ", ".join(str(b) for b in range(start_block, start_block + head_n))
    tail_start = end_block - tail_n + 1
    tail = ", ".join(str(b) for b in range(tail_start, end_block + 1))
    return f"{head}, ..., {tail}"


# Regex to strip ANSI color codes from forge output before saving to disk.
# Forge emits colored output (e.g. green PASS, red FAIL) which is great in
# the live console but noisy in a saved log file or when copy-pasted to AI.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


# Heuristics that strongly suggest a transient RPC problem rather than a
# real on-chain failure of the simSolverCall. Surfacing this distinction
# saves the user from chasing a phantom bug when they really just need to
# retry or pass `--rpc` with a more reliable archive endpoint.
_RPC_FAILURE_HINTS = (
    "missing trie node",
    "required historical state",
    "header not found",
    "block not found",
    "HTTP error 5",  # 500/502/503/504
    "HTTP error 429",
    "Temporary internal error",
    "tls handshake",
    "connection reset",
    "connection refused",
    "rate limit",
    "request timeout",
    "EOF",
)


def _summarize_forge_output(plain_text: str) -> dict:
    """Pull the headline status fields out of a captured forge -vvvv run.

    We rely on:
      - The `[PASS]` / `[FAIL: <reason>] <fn>()` line forge prints per test.
        `<fn>` may be `test_replay` (real test failure) or `setUp` (fork or
        infrastructure failure before the test even ran).
      - The `console.log` lines emitted by our own template (`success:`,
        `simResult:`, `outcome:` plus the decoded text variants).
    """
    info: dict = {
        "forge_status": None,
        "fail_reason": None,
        "fail_phase": None,
        "rpc_issue": False,
    }

    pass_m = re.search(r"\[PASS\]\s+test_replay", plain_text)
    fail_m = re.search(
        r"\[FAIL[:.]?\s*([^\]]*)\]\s+(test_replay|setUp)\b",
        plain_text,
    )
    if pass_m:
        info["forge_status"] = "PASS"
    elif fail_m:
        info["forge_status"] = "FAIL"
        info["fail_reason"] = fail_m.group(1).strip() or None
        info["fail_phase"] = fail_m.group(2)

    if any(hint.lower() in plain_text.lower() for hint in _RPC_FAILURE_HINTS):
        info["rpc_issue"] = True

    for key in ("success", "simResult", "outcome"):
        m = re.search(rf"^\s*{key}:\s*(.+)$", plain_text, re.MULTILINE)
        if m:
            info[key] = m.group(1).strip()

    sim_decoded = re.findall(r"simResult\s*->\s*(\w+)", plain_text)
    if sim_decoded:
        info["simResult_decoded"] = sim_decoded[-1]

    outcome_decoded = re.findall(r"outcome\s*->\s*([^\n\r]+)", plain_text)
    if outcome_decoded:
        info["outcome_decoded"] = [o.strip() for o in outcome_decoded]

    return info


def _run_forge_replay_check(
    *,
    project_root: str,
    match_path: str,
    rpc_url: str,
    block: int,
    timeout_sec: int,
) -> dict:
    """Run one replay test at a specific block and return pass/fail metadata."""
    cmd = [
        "forge",
        "test",
        "--match-path",
        match_path,
        "--match-test",
        "test_replay",
        "--fork-url",
        rpc_url,
        "--fork-block-number",
        str(block),
    ]
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "passed": False,
            "rpc_issue": False,
            "reason": f"forge timed out after {timeout_sec}s",
            "elapsed_s": time.time() - started,
        }
    except OSError as e:
        return {
            "ok": False,
            "passed": False,
            "rpc_issue": False,
            "reason": f"failed to launch forge: {e}",
            "elapsed_s": time.time() - started,
        }

    plain = _ANSI_RE.sub("", (proc.stdout or "") + "\n" + (proc.stderr or ""))
    summary = _summarize_forge_output(plain)

    passed = proc.returncode == 0 and summary.get("forge_status") != "FAIL"
    reason = summary.get("fail_reason")
    if not passed and not reason:
        tail = [line.strip() for line in plain.splitlines() if line.strip()]
        if tail:
            reason = tail[-1][:120]

    return {
        "ok": True,
        "passed": passed,
        "rpc_issue": bool(summary.get("rpc_issue")),
        "reason": reason,
        "elapsed_s": time.time() - started,
    }


def cmd_parse(args: argparse.Namespace) -> None:
    """Parse and print basic decoded pData fields only."""
    pdata = parse_pdata(args.pdata)
    chain = detect_chain(pdata)

    _print_header("pData Summary")
    _print_field("Chain", f"{chain.name} ({chain.chain_id})")
    if pdata.auction_id:
        _print_field("Auction ID", pdata.auction_id)
    if pdata.result_text:
        _print_field("Sorter Result", pdata.result_text[:80])
    _print_field("Simulator", pdata.simulator or chain.simulator)
    _print_field("Calldata Size", f"{len(pdata.raw_hex) // 2} bytes")

    if pdata.user_op:
        print()
        _print_header("UserOperation")
        uo = pdata.user_op
        _print_field("from", uo.from_addr)
        _print_field("to (Atlas)", uo.to_addr)
        _print_field("gas", str(uo.gas))
        _print_field("maxFeePerGas", f"{uo.max_fee_per_gas} ({uo.max_fee_per_gas / 1e9:.4f} Gwei)")
        _print_field("deadline", str(uo.deadline))
        _print_field("dapp", uo.dapp)
        _print_field("control", uo.control)
        _print_field("callConfig", str(uo.call_config))
        _print_field("data selector", uo.data[:10] if len(uo.data) > 2 else "(empty)")
        _print_field("data length", f"{(len(uo.data) - 2) // 2} bytes")

    if pdata.solver_op:
        print()
        _print_header("SolverOperation")
        so = pdata.solver_op
        _print_field("from (EOA)", so.from_addr)
        _print_field("to (Atlas)", so.to_addr)
        _print_field("gas", str(so.gas))
        _print_field("maxFeePerGas", str(so.max_fee_per_gas))
        _print_field("deadline", str(so.deadline))
        _print_field("solver contract", so.solver)
        _print_field("control", so.control)
        _print_field("userOpHash", so.user_op_hash[:18] + "...")
        _print_field("bidToken", so.bid_token)
        _print_field("bidAmount", str(so.bid_amount))
        _print_field("data selector", so.data[:10] if len(so.data) > 2 else "(empty)")
        _print_field("data length", f"{(len(so.data) - 2) // 2} bytes")

    if pdata.dapp_op:
        print()
        _print_header("DAppOperation")
        do = pdata.dapp_op
        _print_field("bundler", do.bundler)
        _print_field("deadline", str(do.deadline))
        _print_field("userOpHash", do.user_op_hash[:18] + "...")

    if pdata.oracle_timestamp or pdata.oracle_report:
        print()
        _print_header("Oracle Report (basic)")
        report = pdata.oracle_report
        ts = pdata.oracle_timestamp or (report.timestamp if report else 0)
        if ts:
            dt = datetime.datetime.utcfromtimestamp(ts)
            _print_field("Observation Time", f"{dt.isoformat()}Z (unix: {ts})")
        if report:
            if report.atlas_wrapper:
                _print_field("Atlas Wrapper", report.atlas_wrapper)
            if report.base_feed:
                _print_field("Base Chainlink Feed", report.base_feed)
            _print_field("Epoch & Round", str(report.epoch_and_round))
            if report.num_signatures is not None:
                _print_field("Signatures", str(report.num_signatures))
            if report.median is not None:
                _print_field("Median (raw int192)", str(report.median))

    if pdata.errors:
        print()
        print("  WARNINGS:")
        for err in pdata.errors:
            print(f"    - {err}")

    print()
    print("  Next step:")
    print(f'    python3 -m atlas_debugger sweep "{args.pdata}" --rpc <RPC_URL>')


def _resolve_block(pdata, chain, args) -> tuple[int, str]:
    """Determine the best block to simulate at, with explanation."""
    if args.block:
        deadline = pdata.user_op.deadline
        return args.block, f"user-specified (deadline - {deadline - args.block})"

    # If user didn't override offset and we have an oracle timestamp, use it
    if pdata.oracle_timestamp and not hasattr(args, '_offset_explicit'):
        oracle_block = find_block_by_timestamp(pdata.oracle_timestamp, chain, getattr(args, 'rpc', None))
        if oracle_block:
            ts = pdata.oracle_timestamp
            dt = datetime.datetime.utcfromtimestamp(ts)
            return oracle_block, f"from oracle timestamp {dt.strftime('%Y-%m-%d %H:%M:%S')}Z"

    deadline = pdata.user_op.deadline
    offset = args.offset or 100
    return deadline - offset, f"deadline - {offset}"


def cmd_simulate(args: argparse.Namespace) -> None:
    """Simulate pData at a specific block."""
    pdata = parse_pdata(args.pdata)
    chain = detect_chain(pdata)
    simulator = pdata.simulator or chain.simulator

    if not pdata.user_op:
        print("ERROR: Failed to decode UserOperation from pData")
        sys.exit(1)

    deadline = pdata.user_op.deadline
    gas_price = pdata.gas_fee_cap or pdata.user_op.max_fee_per_gas

    block, block_source = _resolve_block(pdata, chain, args)

    rpcs = get_archive_rpcs(chain, args.rpc)

    _print_header("Simulation")
    _print_field("Chain", chain.name)
    _print_field("RPCs to try", f"{len(rpcs)} providers")
    _print_field("Simulator", simulator)
    _print_field("Deadline", str(deadline))
    _print_field("Block", f"{block} ({block_source})")
    _print_field("Gas Price", str(gas_price))
    if pdata.solver_op:
        _print_field("Solver", pdata.solver_op.solver)
        bid_eth = pdata.solver_op.bid_amount / 1e18
        _print_field("Bid Amount", f"{bid_eth:.10f} ETH")
    print()
    print("  Running simulation (auto-trying multiple RPCs)...")

    result = simulate_at_block(
        calldata=pdata.calldata,
        simulator=simulator,
        chain=chain,
        block=block,
        gas_price=gas_price,
        user_rpc=args.rpc,
        verbose=True,
    )

    print()
    _print_header("Result")
    _print_field("Success", str(result.success))
    _print_field("Result", f"{result.result_name} ({result.result_code})")
    _print_field("Outcome", str(result.outcome))
    if result.rpc_used:
        _print_field("RPC Used", result.rpc_used[:60])

    if result.outcome_bits:
        _print_field("Outcome Flags", ", ".join(result.outcome_bits))

    if result.result_code == 1:
        code_name = VERIFICATION_FAIL_CODES.get(result.outcome, "Unknown")
        _print_field("Verification Fail", code_name)

    if result.error:
        _print_field("Error", result.error[:100])

    print()
    _print_header("Diagnosis")
    if result.error:
        print(f"  All RPCs failed: {result.error[:120]}")
        print()
        print("  Possible fixes:")
        print("    - Retry the command (free RPCs are flaky)")
        print("    - Provide your own archive RPC: --rpc <url>")
        print("    - Get a free Alchemy key: https://www.alchemy.com/")
    elif result.passed:
        print("  Simulation PASSED at this block.")
        print("  The Sorter may have simulated at a different (later) block.")
        print()
        print("  Next: try `sweep` to find the exact block where it starts failing.")
    elif result.result_code == 3:
        print("  UserOp failed during simulation (UserOpSimFail).")
        print("  The oracle price update likely reverted (e.g., StaleReport).")
        print()
        print("  Next steps:")
        print("    1. Use `sweep` to find the exact block where failure starts.")
        print("    2. Use `trace` to get the full execution trace.")
    elif result.result_code == 4:
        if result.outcome & (1 << 17):
            print("  Solver's internal logic reverted (SolverOpReverted).")
            print("  Common causes:")
            print("    - Liquidation target not undercollateralized")
            print("    - Insufficient token balance for WETH.withdraw()")
            print("    - Swap slippage exceeded")
            print()
            print("  Next: use `trace` to get the exact revert reason.")
        elif result.outcome & (1 << 19):
            print("  Solver did not pay the bid amount (BidNotPaid).")
        elif result.outcome & (1 << 8):
            print("  Solver has insufficient escrow to cover gas liability.")
        else:
            print(f"  Solver simulation failed with outcome bits: {result.outcome_bits}")
    elif result.result_code == 1:
        code_name = VERIFICATION_FAIL_CODES.get(result.outcome, "Unknown")
        print(f"  Verification failed: {code_name}")
        if result.outcome == 13:
            print("  The simulation block is past the UserOp deadline.")
            print("  Try a larger --offset value or specify --block before the deadline.")
        elif result.outcome == 10:
            print("  tx.gasprice exceeds userOp.maxFeePerGas.")
    else:
        print(f"  Simulation failed with result {result.result_name}.")


def cmd_sweep(args: argparse.Namespace) -> None:
    """Find block ranges and sweep pass/fail quickly via eth_call."""
    pdata = parse_pdata(args.pdata)
    chain = detect_chain(pdata)

    if not pdata.user_op:
        print("ERROR: Failed to decode UserOperation")
        sys.exit(1)

    simulator = pdata.simulator or chain.simulator
    deadline = pdata.user_op.deadline
    gas_price = pdata.gas_fee_cap or pdata.user_op.max_fee_per_gas
    base = os.path.splitext(os.path.basename(args.pdata))[0]
    project_root = _project_root()
    test_dir = os.path.join(project_root, "test")
    test_path = os.path.join(project_root, "test", f"{base}.t.sol")
    rpc_candidates = get_archive_rpcs(chain, args.rpc)
    rpc_url = rpc_candidates[0] if rpc_candidates else (args.rpc or (chain.rpcs[0] if chain.rpcs else ""))
    if not rpc_url:
        print("ERROR: No RPC endpoint available. Pass --rpc <ARCHIVE_RPC_URL>.")
        sys.exit(1)

    print()
    _print_header("Sweep Precheck")
    print("  Looking for landed metacall...")

    oracle_block = None
    if pdata.oracle_timestamp:
        oracle_block = find_block_by_timestamp(pdata.oracle_timestamp, chain, args.rpc)

    winners = []
    if pdata.solver_op:
        winners, _ = find_landed_metacall(
            pdata,
            chain,
            user_rpc=args.rpc,
            block_window_after_deadline=args.after,
            block_window_before_oracle=args.before,
            verbose=False,
            oracle_block=oracle_block,
        )

    landed_yes = bool(winners)
    _print_field("Landed Metacall", "FOUND" if landed_yes else "NOT FOUND")
    if landed_yes:
        _print_field("Landed Block", str(winners[0].block_number))
        _print_field("Landed Tx", winners[0].tx_hash)

    ts = pdata.oracle_timestamp
    if ts:
        dt = datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S UTC")
        _print_field("Oracle Timestamp", f"{ts} ({dt})")
    else:
        _print_field("Oracle Timestamp", "(none)")
    _print_field("Timestamp Block", str(oracle_block) if oracle_block is not None else "(unresolved)")

    feed_description: str | None = None
    if pdata.oracle_report and pdata.oracle_report.base_feed:
        info = describe_feed(pdata.oracle_report.base_feed, chain, args.rpc)
        feed_description = info.get("description")

    if winners:
        landed_block = winners[0].block_number
        fork_block = landed_block - 1
        fork_source = f"landed metacall block {landed_block} - 1"
    elif oracle_block is not None:
        fork_block = oracle_block - 1
        fork_source = f"oracle block {oracle_block} - 1 (no landing tx found)"
    else:
        fork_block = deadline - 100
        fork_source = f"deadline ({deadline}) - 100 (no oracle, no landing tx)"

    # sweep owns block-finding and replay-test generation.
    os.makedirs(test_dir, exist_ok=True)
    path = write_test(
        pdata=pdata,
        chain=chain,
        oracle_block=oracle_block,
        output_path=test_path,
        source_file=args.pdata,
        fork_block=fork_block,
        feed_description=feed_description,
    )
    match_path = os.path.relpath(path, project_root)

    if winners:
        landed_block = winners[0].block_number
        end_block = landed_block - 1
        start_block = max(0, end_block - max(args.lookback, 1) + 1)
        mode = f"userOp landed at block {landed_block}; sweeping previous {end_block - start_block + 1} blocks"
    elif oracle_block is not None:
        start_block = oracle_block
        end_block = deadline
        mode = f"userOp not found on-chain; sweeping oracle block -> deadline ({oracle_block}..{deadline})"
    else:
        end_block = deadline
        start_block = max(0, end_block - max(args.lookback, 1) + 1)
        mode = (
            "userOp not found and oracle timestamp unavailable; "
            f"fallback to deadline lookback ({start_block}..{end_block})"
        )

    if end_block < start_block:
        print("ERROR: invalid sweep block range.")
        sys.exit(1)

    total_blocks = end_block - start_block + 1
    _print_field("Blocks To Test", f"{start_block} .. {end_block} ({total_blocks} blocks)")
    _print_field("Block Targets", _format_block_targets(start_block, end_block))
    print()

    _print_header("Sweep (eth_call)")
    _print_field("Chain", chain.name)
    _print_field("Replay Test", os.path.relpath(path, project_root))
    _print_field("Generated Fork Block", f"{fork_block} ({fork_source})")
    _print_field("Mode", mode)
    _print_field("Block Range", f"{start_block} .. {end_block} ({total_blocks} blocks)")
    _print_field("Engine", "eth_call simSolverCall")
    _print_field("Fork RPC", rpc_url[:80] + ("..." if len(rpc_url) > 80 else ""))
    print()

    prev_passed: bool | None = None
    passes: list[int] = []
    fails: list[int] = []
    boundaries: list[int] = []

    for idx, block in enumerate(range(start_block, end_block + 1), start=1):
        started = time.time()
        sim_result = simulate_at_block(
            calldata=pdata.calldata,
            simulator=simulator,
            chain=chain,
            block=block,
            gas_price=gas_price,
            user_rpc=args.rpc,
            timeout=args.timeout,
            retries_per_rpc=3,
            verbose=False,
        )
        elapsed_s = time.time() - started
        passed = bool(sim_result.passed)
        status = "PASS" if passed else "FAIL"
        if sim_result.error:
            status += " (RPC?)"

        marker = ""
        if prev_passed is not None and prev_passed != passed:
            marker = " <-- BOUNDARY"
            boundaries.append(block)

        reason_short = ""
        if sim_result.error:
            reason_short = sim_result.error[:120]
        elif not passed:
            bits = ", ".join(sim_result.outcome_bits) if sim_result.outcome_bits else "none"
            reason_short = (
                f"{sim_result.result_name}, outcome={sim_result.outcome}, bits=[{bits}]"
            )

        line = (
            f"  [{idx}/{total_blocks}] Block {block}: {status}{marker}  [{elapsed_s:.1f}s]"
        )
        if reason_short:
            line += f" | {reason_short}"
        print(line)

        if passed:
            passes.append(block)
        else:
            fails.append(block)
        prev_passed = passed

        if args.delay > 0 and block < end_block:
            time.sleep(args.delay)

    print()
    _print_header("Sweep Summary")
    _print_field("PASS", str(len(passes)))
    _print_field("FAIL", str(len(fails)))
    if passes:
        _print_field("First PASS", str(passes[0]))
        _print_field("Last PASS", str(passes[-1]))
    if fails:
        _print_field("First FAIL", str(fails[0]))
        _print_field("Last FAIL", str(fails[-1]))
    if boundaries:
        _print_field("Boundary Blocks", ", ".join(str(b) for b in boundaries))
    print()
    print("  Run full trace on a chosen block and save output to a log file:")
    print(f"    cd {project_root}")
    print("    mkdir -p logs")
    print(f"    forge test --match-path {match_path} --match-test test_replay -vvvv \\")
    print(f"      --fork-url {rpc_url} \\")
    print("      --fork-block-number <YOUR_BLOCK> \\")
    print(f"      > logs/replay_{base}_<YOUR_BLOCK>.log 2>&1")


def cmd_trace(args: argparse.Namespace) -> None:
    """Trace pData execution and analyze the revert reason."""
    pdata = parse_pdata(args.pdata)
    chain = detect_chain(pdata)
    simulator = pdata.simulator or chain.simulator

    if not pdata.user_op:
        print("ERROR: Failed to decode UserOperation from pData")
        sys.exit(1)

    deadline = pdata.user_op.deadline
    gas_price = pdata.gas_fee_cap or pdata.user_op.max_fee_per_gas

    block, block_source = _resolve_block(pdata, chain, args)

    rpcs = get_archive_rpcs(chain, args.rpc)

    _print_header("Trace")
    _print_field("Chain", chain.name)
    _print_field("RPCs to try", f"{len(rpcs)} providers")
    _print_field("Simulator", simulator)
    _print_field("Deadline", str(deadline))
    _print_field("Block", f"{block} ({block_source})")
    _print_field("Gas Price", str(gas_price))
    if pdata.solver_op:
        _print_field("Solver", pdata.solver_op.solver)
    print()

    # Step 1: Simulation
    print("  Step 1: Running simulation to confirm failure...")
    sim_result = simulate_at_block(
        calldata=pdata.calldata,
        simulator=simulator,
        chain=chain,
        block=block,
        gas_price=gas_price,
        user_rpc=args.rpc,
        verbose=True,
    )

    if sim_result.error:
        print(f"  Simulation failed across all RPCs: {sim_result.error[:80]}")
        print("  Will still attempt trace...")
    elif sim_result.passed:
        _print_field("Sim Result", f"{sim_result.result_name} ({sim_result.result_code})")
        print()
        print("  Simulation PASSED at this block - no failure to trace.")
        print("  Try a block closer to the deadline where the failure occurs.")
        return
    else:
        _print_field("Sim Result", f"{sim_result.result_name} ({sim_result.result_code})")
        if sim_result.outcome_bits:
            _print_field("Outcome Flags", ", ".join(sim_result.outcome_bits))
        if sim_result.rpc_used:
            _print_field("RPC Used", sim_result.rpc_used[:60])

    # Step 2: Trace
    print()
    print("  Step 2: Fetching debug_traceCall (trying multiple RPCs)...")
    output_file = args.output or None
    trace = trace_at_block(
        calldata=pdata.calldata,
        simulator=simulator,
        chain=chain,
        block=block,
        gas_price=gas_price,
        user_rpc=args.rpc,
        output_file=output_file,
        verbose=True,
    )

    if not trace.success:
        print(f"  debug_traceCall failed: {trace.error[:80]}")
        print()
        print("  Step 2b: Falling back to Foundry local trace (forge test -vvvv)...")
        trace = forge_trace_at_block(
            calldata=pdata.calldata,
            simulator=simulator,
            chain=chain,
            block=block,
            gas_price=gas_price,
            user_rpc=args.rpc,
            verbose=True,
        )

        if not trace.success:
            print(f"  Foundry trace also failed: {trace.error}")
            print()
            _print_header("Troubleshooting")
            print("  Both debug_traceCall and forge trace failed.")
            print()
            print("  Options:")
            print("    1. Retry (free RPCs are flaky, may work next time)")
            print("    2. Use Alchemy with debug API add-on (free tier available)")
            print("       --rpc https://arb-mainnet.g.alchemy.com/v2/<YOUR_KEY>")
            print("    3. Ensure `forge` is installed: curl -L https://foundry.paradigm.xyz | bash")
            return

    if trace.rpc_used:
        _print_field("Trace RPC", trace.rpc_used[:60])
    if output_file:
        print(f"  Raw trace saved to: {output_file}")

    # Step 3: Call tree
    print()
    max_depth = args.depth or 8
    _print_header(f"Call Tree (max depth={max_depth})")
    print_call_tree(trace.root_frame, max_depth=max_depth)

    # Step 4: Deepest revert
    deepest = find_deepest_revert(trace.root_frame)
    if deepest:
        print()
        _print_header("Deepest Revert")
        _print_field("Type", deepest.call_type)
        _print_field("To", deepest.to_addr)
        _print_field("Selector", deepest.selector)
        _print_field("Depth", str(deepest.depth))
        _print_field("Gas Used", str(deepest.gas_used))
        if deepest.error:
            _print_field("Error", deepest.error)
        if deepest.revert_reason:
            _print_field("Revert Reason", deepest.revert_reason)
        if deepest.output_data and deepest.output_data != "0x":
            out_display = deepest.output_data
            if len(out_display) > 120:
                out_display = out_display[:120] + "..."
            _print_field("Output", out_display)

    # Step 5: Auto-analyze
    print()
    solver_addr = pdata.solver_op.solver if pdata.solver_op else None
    diagnoses = analyze_trace(trace, solver_addr=solver_addr)

    _print_header("Analysis")
    print(format_diagnoses(diagnoses))


def _project_root() -> str:
    """Find the atlas-pdata-debugger project root (where foundry.toml lives)."""
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    if os.path.isfile(os.path.join(root, "foundry.toml")):
        return root
    return os.getcwd()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="atlas-debug",
        description="Atlas pData Debugger - diagnose simulation failures for searchers",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # parse
    p_parse = subparsers.add_parser("parse", help="Parse and display basic decoded pData fields")
    p_parse.add_argument("pdata", help="Path to pData file")

    # simulate
    p_sim = subparsers.add_parser("simulate", help="Simulate pData at a specific block")
    p_sim.add_argument("pdata", help="Path to pData file")
    p_sim.add_argument("--rpc", help="Custom RPC URL (optional, built-in archive RPCs used by default)")
    p_sim.add_argument("--block", type=int, help="Specific block number")
    p_sim.add_argument("--offset", type=int, default=100, help="Blocks before deadline (default: 100)")

    # sweep
    p_sweep = subparsers.add_parser("sweep", help="Sweep pass/fail across blocks using eth_call (fast) and generate test/<pdata>.t.sol")
    p_sweep.add_argument("pdata", help="Path to pData file")
    p_sweep.add_argument("--rpc", help="Custom RPC URL (optional)")
    p_sweep.add_argument("--lookback", type=int, default=30, help="When landed tx exists, scan this many blocks before it (default: 30)")
    p_sweep.add_argument("--before", type=int, default=5, help="Extra blocks to scan before the oracle block (default: 5)")
    p_sweep.add_argument("--after", type=int, default=5, help="Extra blocks to scan after the deadline (default: 5)")
    p_sweep.add_argument("--delay", type=float, default=0.0, help="Delay between per-block checks in seconds")
    p_sweep.add_argument("--timeout", type=int, default=30, help="Timeout (seconds) for each per-block eth_call")

    # trace
    p_trace = subparsers.add_parser("trace", help="Trace execution and auto-analyze revert reason")
    p_trace.add_argument("pdata", help="Path to pData file")
    p_trace.add_argument("--rpc", help="Custom RPC URL (optional, must support debug_traceCall)")
    p_trace.add_argument("--block", type=int, help="Specific block number")
    p_trace.add_argument("--offset", type=int, default=100, help="Blocks before deadline (default: 100)")
    p_trace.add_argument("--output", "-o", help="Save raw trace JSON to file")
    p_trace.add_argument("--depth", type=int, default=8, help="Max call tree display depth (default: 8)")

    args = parser.parse_args()

    if args.command == "parse":
        cmd_parse(args)
    elif args.command == "simulate":
        cmd_simulate(args)
    elif args.command == "sweep":
        cmd_sweep(args)
    elif args.command == "trace":
        cmd_trace(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
