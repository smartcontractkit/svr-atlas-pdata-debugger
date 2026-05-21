"""CLI entry point for atlas-pdata-debugger."""

from __future__ import annotations

import argparse
import datetime
import os
import re
import shutil
import subprocess
import sys
import time

from .analyzer import analyze_trace, format_diagnoses
from .chain import detect_chain
from .constants import RESULT_NAMES, VERIFICATION_FAIL_CODES
from .find_tx import find_landed_metacall
from .forge_gen import write_test
from .foundry_tracer import forge_trace_at_block
from .parser import ParsedPData, parse_pdata
from .report import write_prompt
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


def _run_auto_trace(
    *,
    project_root: str,
    match_path: str,
    fork_block: int,
    fork_source: str,
    rpc_url: str,
    base: str,
    pdata: ParsedPData,
    chain,
    feed_description: str | None,
    pdata_filename: str,
) -> int:
    """Invoke `forge test -vvvv` for the freshly generated replay test.

    Streams forge's output to the console live AND captures it (with ANSI
    codes stripped) into `out/<base>.trace.log` so the user has both an
    interactive view and a saved artifact. Then renders an AI-ready prompt
    Markdown file at `out/<base>.prompt.md` that bundles the parsed pData
    summary, chain-specific context, the forge result headline and pointers
    to both the trace and the upstream Atlas source code.

    Returns forge's exit code (0 on success, non-zero on test failure or
    infrastructure error). Note that for `simSolverCall` replays, "test
    failure" usually means the simulator's `(success, simResult, outcome)`
    return values indicate a problem — which is precisely what the user is
    debugging — so the trace artifact is still produced and useful.
    """
    if shutil.which("forge") is None:
        print("  [auto-trace] `forge` not found in PATH. Install Foundry to enable auto-trace:")
        print("              curl -L https://foundry.paradigm.xyz | bash && foundryup")
        return 127

    trace_dir = os.path.join(project_root, "out")
    os.makedirs(trace_dir, exist_ok=True)
    trace_path = os.path.join(trace_dir, f"{base}.trace.log")

    cmd = [
        "forge", "test",
        "--match-path", match_path,
        "--match-test", "test_replay",
        "-vvvv",
        "--fork-url", rpc_url,
        "--fork-block-number", str(fork_block),
    ]

    rpc_short = rpc_url[:60] + "..." if len(rpc_url) > 60 else rpc_url
    _print_field("Trace File", os.path.relpath(trace_path, project_root))
    _print_field("Fork RPC", rpc_short)
    print()
    print("  Running: " + " ".join(cmd))
    print("  (forge test on a forked archive node typically takes 10-60s)")
    print("-" * 60)

    started = time.time()
    plain_chunks: list[str] = []
    try:
        with open(trace_path, "w", encoding="utf-8") as logf, subprocess.Popen(
            cmd,
            cwd=project_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        ) as proc:
            assert proc.stdout is not None
            for line in proc.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                plain = _ANSI_RE.sub("", line)
                logf.write(plain)
                plain_chunks.append(plain)
            proc.wait()
            exit_code = proc.returncode
    except KeyboardInterrupt:
        print("\n  [auto-trace] Interrupted by user.")
        return 130
    except OSError as e:
        print(f"  [auto-trace] Failed to launch forge: {e}")
        return 1

    elapsed = time.time() - started
    print("-" * 60)

    captured = "".join(plain_chunks)
    summary = _summarize_forge_output(captured)

    prompt_path: str | None = None
    try:
        prompt_path = write_prompt(
            project_root=project_root,
            base=base,
            pdata=pdata,
            chain=chain,
            fork_block=fork_block,
            fork_source=fork_source,
            feed_description=feed_description,
            pdata_filename=pdata_filename,
            trace_path=trace_path,
            summary=summary,
        )
    except Exception as e:
        print(f"  [auto-trace] WARN: failed to write prompt file: {e}")

    print()
    _print_header("Auto-Trace Summary")
    _print_field("Elapsed", f"{elapsed:.1f}s")
    _print_field("Exit Code", str(exit_code))
    if summary.get("forge_status"):
        line = summary["forge_status"]
        if summary.get("fail_phase"):
            line += f" in {summary['fail_phase']}()"
        if summary.get("fail_reason"):
            reason = summary["fail_reason"]
            if len(reason) > 120:
                reason = reason[:120] + "..."
            line += f" — {reason}"
        _print_field("forge", line)
    if summary.get("simResult_decoded"):
        _print_field("simResult", summary["simResult_decoded"])
    if summary.get("outcome_decoded"):
        _print_field("outcome bits", ", ".join(summary["outcome_decoded"]))
    elif summary.get("outcome"):
        _print_field("outcome (raw)", summary["outcome"])
    _print_field("Trace Saved", os.path.relpath(trace_path, project_root))
    if prompt_path:
        _print_field("AI Prompt", os.path.relpath(prompt_path, project_root))

    print()
    if summary.get("rpc_issue") and summary.get("forge_status") != "PASS":
        print("  Tip: the failure looks like a transient RPC issue (HTTP 5xx,")
        print("       'missing trie node', or rate-limit). Retry, or pass")
        print("       `--rpc <YOUR_ARCHIVE_RPC>` for a more reliable endpoint.")
    elif summary.get("forge_status") == "PASS":
        print("  Tip: the simulation passed at this fork block. If the on-chain")
        print("       metacall failed, try a block closer to the deadline.")
    else:
        if prompt_path:
            rel = os.path.relpath(prompt_path, project_root)
            print(f"  Tip: drop `{rel}` AND the trace log into your AI chat —")
            print("       the prompt already includes protocol context, chain")
            print("       config, pData summary, and concrete questions.")
        else:
            print("  Tip: paste the saved trace file into an AI chat together with")
            print("       the pData summary above for fastest root-cause analysis.")
    return exit_code


def cmd_parse(args: argparse.Namespace) -> None:
    """Parse and display pData fields."""
    pdata = parse_pdata(args.pdata)
    chain = detect_chain(pdata)
    oracle_block: int | None = None
    feed_description: str | None = None

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
        _print_field("maxFeePerGas", f"{so.max_fee_per_gas}")
        _print_field("deadline", str(so.deadline))
        _print_field("solver contract", so.solver)
        _print_field("control", so.control)
        _print_field("userOpHash", so.user_op_hash[:18] + "...")
        _print_field("bidToken", so.bid_token)
        bid_eth = so.bid_amount / 1e18
        _print_field("bidAmount", f"{so.bid_amount} ({bid_eth:.10f} ETH)")
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
        _print_header("Oracle Report (Chainlink transmit)")
        report = pdata.oracle_report
        ts = pdata.oracle_timestamp or (report.timestamp if report else 0)

        if ts:
            dt = datetime.datetime.utcfromtimestamp(ts)
            _print_field("Observation Time", f"{dt.isoformat()}Z (unix: {ts})")
        if pdata.user_op:
            _print_field("Deadline Block", str(pdata.user_op.deadline))

        if report:
            feed_decimals: int | None = None
            if report.base_feed:
                print("  Resolving base feed description via RPC...")
                info = describe_feed(report.base_feed, chain, getattr(args, "rpc", None))
                feed_description = info.get("description")
                feed_decimals = info.get("decimals")

            if report.atlas_wrapper:
                _print_field("Atlas Wrapper", f"{report.atlas_wrapper}  [stores OEV price]")
            if report.base_feed:
                label = report.base_feed
                if feed_description:
                    label += f"  [{feed_description}]"
                _print_field("Base Chainlink Feed", label)
            if feed_decimals is not None:
                _print_field("Feed Decimals", str(feed_decimals))

            _print_field("Raw Report Context", report.raw_report_context)
            _print_field("Epoch & Round", str(report.epoch_and_round))
            _print_field("Raw Observers", report.raw_observers)
            if report.observer_indices:
                idx_str = ",".join(str(i) for i in report.observer_indices)
                _print_field("Observer Indices", f"[{idx_str}] ({len(report.observer_indices)} oracles)")
            if report.num_signatures is not None:
                _print_field("Signatures", str(report.num_signatures))
            if report.observations:
                _print_field("Num Observations", str(len(report.observations)))
                if report.median is not None:
                    med = report.median
                    med_line = f"{med}"
                    if feed_decimals is not None:
                        med_line += f"  ({feed_decimals} dec: {med / (10 ** feed_decimals):.{feed_decimals}f})"
                    else:
                        med_line += f"  (8 dec: {med / 1e8:.8f}, 18 dec: {med / 1e18:.10f})"
                    _print_field("Median (raw int192)", med_line)
                print()
                print("  Observations (sorted ascending by Chainlink; in raw int192):")
                for i, obs in enumerate(report.observations):
                    marker = "  <- median" if i == len(report.observations) // 2 else ""
                    if feed_decimals is not None:
                        scaled = f"{feed_decimals}d: {obs / (10 ** feed_decimals):.{feed_decimals}f}"
                    else:
                        scaled = f"8d: {obs / 1e8:.6f}, 18d: {obs / 1e18:.8f}"
                    print(f"    [{i:2d}] {obs}  ({scaled}){marker}")

        if not ts:
            ts = 0
        if ts:
            print()
            print("  Finding the block for this timestamp...")
            oracle_block = find_block_by_timestamp(ts, chain, getattr(args, "rpc", None))
        else:
            oracle_block = None
        if oracle_block:
            deadline = pdata.user_op.deadline if pdata.user_op else 0
            gap = deadline - oracle_block
            _print_field("Oracle Block", str(oracle_block))
            _print_field("Gap to Deadline", f"{gap} blocks (~{gap * chain.block_time_sec:.0f}s)")
            print()
            print(f"  Recommended simulation block: {oracle_block}")
            print(f"  Usage: python3 -m atlas_debugger simulate <pdata> --block {oracle_block}")
        else:
            print("  Could not resolve block (RPC unavailable).")
            print(f"  You can run: cast find-block {ts} --rpc-url <rpc>")

    if pdata.errors:
        print()
        print("  WARNINGS:")
        for err in pdata.errors:
            print(f"    - {err}")

    landing_block: int | None = None
    if not getattr(args, "no_find_tx", False):
        if pdata.user_op and pdata.solver_op:
            print()
            _print_header("Find Landed Metacall Tx")
            winners = _run_find_tx(
                pdata,
                chain,
                user_rpc=getattr(args, "rpc", None),
                before=getattr(args, "before", 5),
                after=getattr(args, "after", 5),
                oracle_block=oracle_block if pdata.oracle_timestamp else None,
                show_header_fields=False,
            )
            if winners:
                landing_block = winners[0].block_number

    if not getattr(args, "no_generate", False) and pdata.user_op:
        print()
        _print_header("Generate Foundry Replay Test")
        if landing_block is not None:
            fork_block = landing_block - 1
            fork_source = f"landed metacall block {landing_block} - 1"
        elif oracle_block is not None:
            fork_block = oracle_block - 1
            fork_source = f"oracle block {oracle_block} - 1 (no landing tx found)"
        else:
            deadline = pdata.user_op.deadline
            fork_block = deadline - 100
            fork_source = f"deadline ({deadline}) - 100 (no oracle, no landing tx)"

        base = os.path.splitext(os.path.basename(args.pdata))[0]
        project_root = _project_root()
        test_dir = os.path.join(project_root, "test")
        output_path = os.path.join(test_dir, f"{base}.t.sol")

        path = write_test(
            pdata=pdata,
            chain=chain,
            oracle_block=oracle_block,
            output_path=output_path,
            source_file=args.pdata,
            fork_block=fork_block,
            feed_description=feed_description,
        )

        rpc_hint = getattr(args, "rpc", None) or (chain.rpcs[0] if chain.rpcs else "<RPC_URL>")
        match_path = os.path.relpath(path, project_root)
        _print_field("Output", os.path.relpath(path))
        _print_field("Fork Block", f"{fork_block}  ({fork_source})")
        print()
        print("  Run with:")
        print(f"    cd {project_root}")
        print(f"    forge test --match-path {match_path} --match-test test_replay -vvvv \\")
        print(f"      --fork-url {rpc_hint} \\")
        print(f"      --fork-block-number {fork_block}")

        if getattr(args, "auto_trace", False):
            print()
            _print_header("Auto-Trace (forge test -vvvv)")
            # Prefer the user-provided --rpc for forking. Otherwise fall back
            # to the first archive RPC (drpc.org-class), which is needed for
            # historical-block forks; chain.rpcs[0] is often the public RPC
            # without archive state and would fail with "missing trie node".
            user_rpc = getattr(args, "rpc", None)
            fork_rpc = user_rpc or get_archive_rpcs(chain, None)[0]
            _run_auto_trace(
                project_root=project_root,
                match_path=match_path,
                fork_block=fork_block,
                fork_source=fork_source,
                rpc_url=fork_rpc,
                base=base,
                pdata=pdata,
                chain=chain,
                feed_description=feed_description,
                pdata_filename=args.pdata,
            )


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
    """Sweep a range of blocks to find the exact pass/fail boundary."""
    pdata = parse_pdata(args.pdata)
    chain = detect_chain(pdata)
    simulator = pdata.simulator or chain.simulator

    if not pdata.user_op:
        print("ERROR: Failed to decode UserOperation")
        sys.exit(1)

    deadline = pdata.user_op.deadline
    gas_price = pdata.gas_fee_cap or pdata.user_op.max_fee_per_gas

    start_offset = args.start or 100
    end_offset = args.end or 90

    _print_header(f"Block Sweep: deadline-{start_offset} to deadline-{end_offset}")
    _print_field("Chain", chain.name)
    _print_field("Deadline", str(deadline))
    print()

    prev_passed = None
    boundary_block = None

    for offset in range(start_offset, end_offset - 1, -1):
        block = deadline - offset
        result = simulate_at_block(
            calldata=pdata.calldata,
            simulator=simulator,
            chain=chain,
            block=block,
            gas_price=gas_price,
            user_rpc=args.rpc,
        )

        status = "PASS" if result.passed else "FAIL"
        marker = ""
        if prev_passed is not None and prev_passed and not result.passed:
            marker = " <-- BOUNDARY"
            boundary_block = block

        rpc_hint = ""
        if result.rpc_used:
            rpc_short = result.rpc_used.split("//")[-1].split("/")[0][:20]
            rpc_hint = f" [{rpc_short}]"

        print(f"  Block {block} (dl-{offset:>3}): {result.result_name:<25} {status}{marker}{rpc_hint}")

        if result.error:
            print(f"         Error: {result.error[:70]}")

        prev_passed = result.passed
        time.sleep(args.delay or 1.5)

    if boundary_block:
        print()
        print(f"  Exact boundary: block {boundary_block} (first failure)")
        print(f"  Last passing:   block {boundary_block - 1}")


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


def _run_find_tx(
    pdata,
    chain,
    user_rpc: str | None,
    before: int,
    after: int,
    oracle_block: int | None = None,
    show_header_fields: bool = True,
):
    """Shared implementation for `find-tx` (used by both `parse` and `find-tx`).

    Returns the list of winning MetacallMatch objects (empty if none found).
    """
    if show_header_fields:
        _print_field("Chain", chain.name)
        _print_field("Atlas", pdata.user_op.to_addr)
        _print_field("UserOp.from", pdata.user_op.from_addr)
        _print_field("UserOpHash", pdata.solver_op.user_op_hash)
        _print_field("Deadline Block", str(pdata.user_op.deadline))
        if pdata.oracle_report and pdata.oracle_report.base_feed:
            _print_field("Base Feed", pdata.oracle_report.base_feed)
        print()

    winners, ctx = find_landed_metacall(
        pdata,
        chain,
        user_rpc=user_rpc,
        block_window_after_deadline=after,
        block_window_before_oracle=before,
        verbose=True,
        oracle_block=oracle_block,
    )

    if ctx.get("oracle_block") and not oracle_block:
        _print_field("Oracle Block", str(ctx["oracle_block"]))
    _print_field("Scan Range", f"{ctx['start_block']} .. {ctx['end_block']}")

    candidates = ctx.get("candidates", [])
    if not candidates:
        print()
        print("  No MetacallResult events for this user in the range.")
        if ctx.get("errors"):
            print("  Errors:")
            for e in ctx["errors"]:
                print(f"    - {e}")
        return []

    print()
    _print_header(f"All MetacallResult Candidates ({len(candidates)})")
    for c in candidates:
        marker = " <-- MATCH (userOpHash found in calldata)" if c in winners else ""
        status = "success" if c.solver_successful else "failed"
        print(
            f"  block {c.block_number}  tx {c.tx_hash}  bundler={c.bundler}  "
            f"{status}{marker}"
        )

    print()
    if not winners:
        _print_header("Result")
        print("  No candidate's calldata contains this userOpHash.")
        print("  This usually means the auction never landed on-chain (no solver won or")
        print("  the bundler gave up before broadcasting).")
        return []

    _print_header(f"Landing Transaction ({len(winners)} match{'es' if len(winners) > 1 else ''})")
    for w in winners:
        _print_field("Block", str(w.block_number))
        _print_field("Tx Hash", w.tx_hash)
        _print_field("Bundler", w.bundler)
        _print_field("Solver Successful", str(w.solver_successful))
        _print_field("ETH Paid to Bundler", f"{w.eth_paid_to_bundler} wei ({w.eth_paid_to_bundler / 1e18:.10f} ETH)")
        _print_field("Net Gas Surcharge", f"{w.net_gas_surcharge} wei")
        print()
    return winners


def cmd_find_tx(args: argparse.Namespace) -> None:
    """Locate the on-chain Atlas metacall tx that actually landed for this pData.

    Filters `MetacallResult(bundler, user, ...)` logs on the Atlas contract by the
    indexed `user` topic (== UserOp.from), in the block range `[oracle_block,
    deadline]`. Then pulls each matching tx and matches the `userOpHash` inside
    the calldata — so even if a different solver won the auction, we still find
    the right transaction.
    """
    pdata = parse_pdata(args.pdata)
    chain = detect_chain(pdata)

    if not pdata.user_op or not pdata.solver_op:
        print("ERROR: pData missing userOp or solverOp.")
        sys.exit(1)

    _print_header("Find Landed Metacall Tx")
    _run_find_tx(pdata, chain, args.rpc, args.before, args.after)


def _project_root() -> str:
    """Find the atlas-pdata-debugger project root (where foundry.toml lives)."""
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    if os.path.isfile(os.path.join(root, "foundry.toml")):
        return root
    return os.getcwd()


def cmd_generate(args: argparse.Namespace) -> None:
    """Generate a Foundry test file from pData."""
    pdata = parse_pdata(args.pdata)
    chain = detect_chain(pdata)

    oracle_block = None
    if pdata.oracle_timestamp:
        print("  Resolving oracle timestamp to block number...")
        oracle_block = find_block_by_timestamp(pdata.oracle_timestamp, chain, args.rpc)

    # Determine output path — default to project's test/ directory
    if args.output:
        output_path = args.output
    else:
        base = os.path.splitext(os.path.basename(args.pdata))[0]
        project_root = _project_root()
        test_dir = os.path.join(project_root, "test")
        output_path = os.path.join(test_dir, f"{base}.t.sol")

    path = write_test(
        pdata=pdata,
        chain=chain,
        oracle_block=oracle_block,
        output_path=output_path,
        source_file=args.pdata,
    )

    deadline = pdata.user_op.deadline if pdata.user_op else 0
    fork_block = oracle_block or (deadline - 100)
    rpc_hint = chain.rpcs[0] if chain.rpcs else "<RPC_URL>"
    project_root = _project_root()

    match_path = os.path.relpath(path, project_root)

    _print_header("Generated Foundry Test")
    _print_field("Output", os.path.relpath(path))
    _print_field("Chain", chain.name)
    _print_field("Fork Block", str(fork_block))
    if pdata.oracle_timestamp:
        dt = datetime.datetime.utcfromtimestamp(pdata.oracle_timestamp)
        _print_field("Oracle Time", dt.strftime("%Y-%m-%d %H:%M:%S UTC"))
    if pdata.solver_op:
        _print_field("Solver", pdata.solver_op.solver)
    print()
    print("  Run with:")
    print(f"    cd {project_root}")
    print(f"    forge test --match-path {match_path} --match-test test_replay -vvvv \\")
    print(f"      --fork-url {rpc_hint} \\")
    print(f"      --fork-block-number {fork_block}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="atlas-debug",
        description="Atlas pData Debugger - diagnose simulation failures for searchers",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # parse
    p_parse = subparsers.add_parser("parse", help="Parse and display pData fields")
    p_parse.add_argument("pdata", help="Path to pData file")
    p_parse.add_argument("--rpc", help="Custom RPC URL (optional, used for oracle timestamp -> block resolution and find-tx)")
    p_parse.add_argument(
        "--no-find-tx", action="store_true",
        help="Skip the on-chain landed-metacall-tx lookup at the end (faster, no eth_getLogs calls)",
    )
    p_parse.add_argument(
        "--no-generate", action="store_true",
        help="Skip auto-generating the Foundry replay test at the end",
    )
    p_parse.add_argument(
        "--auto-trace", action="store_true",
        help=(
            "After generating the test file, immediately invoke `forge test -vvvv` "
            "with the right --fork-url/--fork-block-number/--match-path. Streams "
            "output to the console and saves a clean copy to out/<pdata>.trace.log "
            "ready to paste into AI for diagnosis."
        ),
    )
    p_parse.add_argument("--before", type=int, default=5, help="find-tx: extra blocks before oracle block (default: 5)")
    p_parse.add_argument("--after", type=int, default=5, help="find-tx: extra blocks after deadline (default: 5)")

    # simulate
    p_sim = subparsers.add_parser("simulate", help="Simulate pData at a specific block")
    p_sim.add_argument("pdata", help="Path to pData file")
    p_sim.add_argument("--rpc", help="Custom RPC URL (optional, built-in archive RPCs used by default)")
    p_sim.add_argument("--block", type=int, help="Specific block number")
    p_sim.add_argument("--offset", type=int, default=100, help="Blocks before deadline (default: 100)")

    # sweep
    p_sweep = subparsers.add_parser("sweep", help="Sweep blocks to find pass/fail boundary")
    p_sweep.add_argument("pdata", help="Path to pData file")
    p_sweep.add_argument("--rpc", help="Custom RPC URL (optional)")
    p_sweep.add_argument("--start", type=int, default=100, help="Start offset from deadline (default: 100)")
    p_sweep.add_argument("--end", type=int, default=90, help="End offset from deadline (default: 90)")
    p_sweep.add_argument("--delay", type=float, default=1.5, help="Delay between RPC calls in seconds")

    # trace
    p_trace = subparsers.add_parser("trace", help="Trace execution and auto-analyze revert reason")
    p_trace.add_argument("pdata", help="Path to pData file")
    p_trace.add_argument("--rpc", help="Custom RPC URL (optional, must support debug_traceCall)")
    p_trace.add_argument("--block", type=int, help="Specific block number")
    p_trace.add_argument("--offset", type=int, default=100, help="Blocks before deadline (default: 100)")
    p_trace.add_argument("--output", "-o", help="Save raw trace JSON to file")
    p_trace.add_argument("--depth", type=int, default=8, help="Max call tree display depth (default: 8)")

    # generate
    p_gen = subparsers.add_parser("generate", help="Generate a Foundry test file (.t.sol) from pData")
    p_gen.add_argument("pdata", help="Path to pData file")
    p_gen.add_argument("--output", "-o", help="Output .t.sol path (default: <pdata_name>.t.sol)")
    p_gen.add_argument("--rpc", help="Custom RPC URL for timestamp resolution")

    # find-tx
    p_find = subparsers.add_parser(
        "find-tx",
        help="Find the on-chain Atlas metacall tx that landed for this pData (any solver)",
    )
    p_find.add_argument("pdata", help="Path to pData file")
    p_find.add_argument("--rpc", help="Custom RPC URL (optional, must support eth_getLogs)")
    p_find.add_argument(
        "--before", type=int, default=5,
        help="Extra blocks to scan before the oracle block (default: 5)",
    )
    p_find.add_argument(
        "--after", type=int, default=5,
        help="Extra blocks to scan after the deadline (default: 5)",
    )

    args = parser.parse_args()

    if args.command == "parse":
        cmd_parse(args)
    elif args.command == "simulate":
        cmd_simulate(args)
    elif args.command == "sweep":
        cmd_sweep(args)
    elif args.command == "trace":
        cmd_trace(args)
    elif args.command == "generate":
        cmd_generate(args)
    elif args.command == "find-tx":
        cmd_find_tx(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
