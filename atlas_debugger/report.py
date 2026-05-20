"""Build an AI-ready Markdown prompt that pairs with the auto-trace log.

The prompt bundles three layers of context that an AI almost always needs to
diagnose an Atlas `simSolverCall` failure but otherwise has to guess at:

1. **Protocol overview** — what Atlas is, how `simSolverCall` works, the
   meaning of the `simResult` enum and the `outcome` bit flags. Without this
   the AI mis-classifies failures (e.g. treats a `BidNotPaid` outcome as a
   generic revert).

2. **Chain-specific reference** — Atlas/Simulator addresses on this chain,
   the canonical block-explorer URL prefix, native gas symbol, and any
   chain quirks that affect interpretation (e.g. Arbitrum L1-vs-L2
   `block.number`).

3. **This run's pData + forge result** — the parsed pData summary (oracle
   pair, deadline, solver bid, etc.) and the headline values we extracted
   from the forge run (`simResult`, decoded `outcome` bits).

The prompt then ends with a checklist of concrete questions for the AI to
answer, and a pointer to the saved trace file (so the AI can read it via
`@out/<pdata>.trace.log` in Cursor or by attachment elsewhere).
"""

from __future__ import annotations

import datetime
import os
from dataclasses import dataclass
from pathlib import Path

from .constants import ChainConfig
from .parser import ParsedPData


@dataclass(frozen=True)
class ChainPromptContext:
    """Per-chain reference info that varies the generated prompt."""

    explorer_url: str
    native_symbol: str
    wrapped_native_addr: str
    wrapped_native_label: str
    notes: tuple[str, ...] = ()


# Chain-specific data woven into the AI prompt. Adding a new chain to the
# project means appending an entry here so the prompt picks up its
# explorer URL, native gas symbol, and any quirks.
_CHAIN_PROMPT_CONTEXT: dict[int, ChainPromptContext] = {
    1: ChainPromptContext(
        explorer_url="https://etherscan.io",
        native_symbol="ETH",
        wrapped_native_addr="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        wrapped_native_label="WETH",
    ),
    42161: ChainPromptContext(
        explorer_url="https://arbiscan.io",
        native_symbol="ETH",
        wrapped_native_addr="0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
        wrapped_native_label="WETH",
        notes=(
            "On Arbitrum, `block.number` inside Foundry forks returns the L1 "
            "block, while `arbBlockNumber()` (precompile `0x64`) returns the "
            "L2 block. The replay test mocks `arbBlockNumber()` to the L2 "
            "fork block so per-block-limit and timing checks behave correctly.",
            "`getPricesInArbGas()` (precompile `0x6C`) is mocked to ~zero L1 "
            "data fees so calldata-cost arithmetic in the gas calculator does "
            "not blow up at the fork point.",
        ),
    ),
    8453: ChainPromptContext(
        explorer_url="https://basescan.org",
        native_symbol="ETH",
        wrapped_native_addr="0x4200000000000000000000000000000000000006",
        wrapped_native_label="WETH",
    ),
    56: ChainPromptContext(
        explorer_url="https://bscscan.com",
        native_symbol="BNB",
        wrapped_native_addr="0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
        wrapped_native_label="WBNB",
        notes=(
            "BSC uses ~3-second blocks. The wrapped native asset is **WBNB** "
            "(not WETH). All native-denominated bid amounts in this run are "
            "BNB, not ETH.",
        ),
    ),
}


_PROTOCOL_OVERVIEW = """\
## Atlas Protocol overview

Atlas is an EVM smart-account / MEV-OEV auction infrastructure where searchers
bid for the right to bundle a `solverOp` next to a user's `userOp`. The
aggregate bundle is called a **metacall** and is simulated via
`Simulator.simSolverCall(bytes pData)`, which decodes pData into:

```solidity
(bool success, uint8 simResult, uint256 outcome) = simulator.call(pData);
```

For **Chainlink SVR (Secondary Value Recovery) feeds**, the userOp.data is a
nested call chain:

```
update(wrapper, bytes) -> forward(baseFeed, bytes) -> transmit(...)
```

This updates the `ChainlinkAtlasWrapper`'s OEV-captured price. The solver's
job is to capture OEV (liquidate, arbitrage, etc.) within the same atomic
metacall and pay a bid back to the bundler.

### Result code reference

`simResult` enum (`uint8`):

| value | name | meaning |
|---|---|---|
| 0 | `Unknown` | Default / uninitialized. |
| 1 | `VerificationSimFail` | Atlas-level validation rejected the bundle (signatures, deadline, dapp config, etc.). The `outcome` value here is a `ValidCallsResult` enum, not a bit set. |
| 2 | `PreOpsSimFail` | DApp `preOps` hook reverted. |
| 3 | `UserOpSimFail` | UserOp execution reverted. For SVR feeds this typically means the oracle update reverted (e.g. `StaleReport`, `OracleUpdateFailed`). |
| 4 | `SolverSimFail` | One of the `outcome` bits below was tripped. |
| 5 | `AllocateValueSimFail` | DApp `allocateValue` hook reverted. |
| 6 | `SimulationPassed` | The bundle would have succeeded on-chain. |

`outcome` bit flags (`uint256`, **only meaningful when `simResult == 4`**):

| bit | name | meaning |
|---|---|---|
| 8 | `InsufficientEscrow` | Solver hasn't bonded enough atlETH to cover its gas liability. |
| 12 | `PerBlockLimit` | Solver already executed within this block. |
| 16 | `PreSolverFailed` | DApp's `preSolverCall` hook reverted. |
| 17 | `SolverOpReverted` | The solver contract's own logic reverted. |
| 18 | `PostSolverFailed` | DApp's `postSolverCall` hook reverted. |
| 19 | `BidNotPaid` | Solver did not transfer the bid to the bundler. |
| 20 | `InvertedBidExceedsCeiling` | (Inverted-bid auctions only.) |
| 21 | `BalanceNotReconciled` | Solver took funds from atlas but did not repay. |
| 22 | `CallbackNotCalled` | Solver did not call `atlas.reconcile(...)`. |
| 23 | `EVMError` | Out-of-gas or non-revert EVM error. |
"""


_ATLAS_REFERENCES = """\
## Where to find Atlas source code

- **FastLane Labs Atlas monorepo**: https://github.com/FastLane-Labs/atlas
  - `src/contracts/atlas/Atlas.sol` — `metacall(...)` entrypoint
  - `src/contracts/atlas/AtlasVerification.sol` — verification logic (governs `simResult==1`)
  - `src/contracts/atlas/Escrow.sol` — `execute`, `_solverOpWrapper`, bid handling, escrow accounting (governs `simResult==4` outcomes)
  - `src/contracts/types/SolverOperation.sol` — `SolverOutcome` bit enum (the `outcome` value in this trace)
  - `src/contracts/types/ConfigTypes.sol` — `CallConfig` flags
  - `src/contracts/dapp/DAppControl.sol` — base for DAppControl modules
  - `src/contracts/helpers/Simulator.sol` — `simSolverCall(...)`
- **Atlas docs**: https://atlas-docs.fastlane.xyz/ (concepts: solver, dapp, bundler, escrow)
- **Chainlink SVR feed onboarding**:
  https://docs.chain.link/data-feeds/svr-feeds/searcher-onboarding-atlas
"""


_QUESTIONS = """\
## What I want you to do

1. **Find the deepest revert.** Walk down the call tree to the leaf call that
   actually reverted, and decode its return data:
     - `0x08c379a0` prefix → `Error(string)`, decode the string.
     - `0x4e487b71` prefix → `Panic(uint256)`, decode the panic code.
     - other 4-byte prefix → custom error; identify the selector and look it
       up if you can, or list the candidate function/error signatures.
2. **Explain why** in plain language, in terms of THIS solver's intent
   (capture OEV from the price update at this block) — not in terms of "the
   call failed".
3. **Categorize the failure** as one of:
     - **Timing**: oracle not yet updated at this block, deadline passed,
       solver competing with another tx in the same block.
     - **Logic**: balance check / slippage / liquidation HF threshold / wrong
       swap path / underpriced bid.
     - **Config**: wrong DApp selector allowlisted, wrong solver registered,
       wrong CallConfig flags.
4. **Concrete next steps**: list the SHORTEST set of follow-ups that would
   confirm the diagnosis — e.g. "retry at block X", "show source of contract
   Y", "check token Z balance at block N", "decode selector 0xabcd1234".

If anything below is unclear or missing, list it explicitly rather than
guessing.
"""


def _addr_link(addr: str | None, explorer_url: str) -> str:
    """Render an address as a clickable Markdown link to the chain's explorer."""
    if not addr:
        return "_(none)_"
    return f"[`{addr}`]({explorer_url}/address/{addr})"


def _is_zero(addr: str | None) -> bool:
    if not addr:
        return True
    try:
        return int(addr, 16) == 0
    except ValueError:
        return True


def build_prompt(
    pdata: ParsedPData,
    chain: ChainConfig,
    fork_block: int,
    fork_source: str,
    feed_description: str | None,
    trace_relpath: str,
    pdata_filename: str,
    summary: dict,
) -> str:
    """Assemble the Markdown prompt body."""
    ctx = _CHAIN_PROMPT_CONTEXT.get(chain.chain_id)
    explorer_url = ctx.explorer_url if ctx else "https://example.invalid"
    native_symbol = ctx.native_symbol if ctx else "ETH"
    wrapped_label = ctx.wrapped_native_label if ctx else "WETH"
    wrapped_addr = ctx.wrapped_native_addr if ctx else ""
    notes = list(ctx.notes) if ctx else []

    base_name = Path(pdata_filename).stem

    chain_table_rows = [
        f"| Atlas | {_addr_link(chain.atlas, explorer_url)} |",
        f"| Simulator | {_addr_link(pdata.simulator or chain.simulator, explorer_url)} |",
    ]
    if wrapped_addr:
        chain_table_rows.append(
            f"| Wrapped native | {wrapped_label} {_addr_link(wrapped_addr, explorer_url)} |"
        )
    chain_table_rows.append(f"| Native gas symbol | {native_symbol} |")
    chain_table_rows.append(f"| Block explorer | {explorer_url} |")
    chain_table_rows.append(f"| Block time | ~{chain.block_time_sec}s |")

    chain_section = (
        f"## Chain configuration: {chain.name} ({chain.chain_id})\n\n"
        "| Field | Value |\n|---|---|\n"
        + "\n".join(chain_table_rows)
        + "\n"
    )
    if notes:
        chain_section += "\n**Chain-specific quirks:**\n\n" + "\n".join(
            f"- {n}" for n in notes
        ) + "\n"

    pdata_rows: list[str] = [f"| Source pData | `{os.path.basename(pdata_filename)}` |"]
    if pdata.auction_id:
        pdata_rows.append(f"| Auction ID | `{pdata.auction_id}` |")

    if pdata.user_op:
        uo = pdata.user_op
        pdata_rows.append(f"| UserEOA | {_addr_link(uo.from_addr, explorer_url)} |")
        # For SVR feeds, userOp.dapp and userOp.control are typically the same
        # address (the DAppControl handles both DApp pre/post hooks and the
        # update routing). To match how the trace labels them via vm.label, we
        # show one combined row when they are equal.
        if uo.control and uo.control.lower() == (uo.dapp or "").lower():
            pdata_rows.append(
                f"| DAppControl (== DApp) | {_addr_link(uo.control, explorer_url)} |"
            )
        else:
            pdata_rows.append(f"| DApp | {_addr_link(uo.dapp, explorer_url)} |")
            pdata_rows.append(
                f"| DAppControl | {_addr_link(uo.control, explorer_url)} |"
            )
        pdata_rows.append(f"| UserOp deadline (block) | {uo.deadline} |")
        pdata_rows.append(
            f"| UserOp gas / maxFeePerGas | {uo.gas} / "
            f"{uo.max_fee_per_gas} ({uo.max_fee_per_gas / 1e9:.4f} Gwei) |"
        )
        pdata_rows.append(f"| CallConfig | `{uo.call_config}` |")

    if pdata.solver_op:
        so = pdata.solver_op
        pdata_rows.append(f"| Solver contract | {_addr_link(so.solver, explorer_url)} |")
        pdata_rows.append(f"| SolverEOA | {_addr_link(so.from_addr, explorer_url)} |")
        if _is_zero(so.bid_token):
            bid_token_display = f"native {native_symbol} (`bidToken=0x0`)"
        else:
            bid_token_display = _addr_link(so.bid_token, explorer_url)
        pdata_rows.append(f"| Bid token | {bid_token_display} |")
        bid_native = so.bid_amount / 1e18
        pdata_rows.append(
            f"| Solver bid | {bid_native:.10f} {native_symbol} ({so.bid_amount} wei) |"
        )
        pdata_rows.append(f"| userOpHash | `{so.user_op_hash}` |")

    if pdata.dapp_op:
        do = pdata.dapp_op
        pdata_rows.append(f"| Bundler | {_addr_link(do.bundler, explorer_url)} |")

    if pdata.oracle_report:
        rep = pdata.oracle_report
        if rep.atlas_wrapper:
            pdata_rows.append(
                f"| ChainlinkAtlasWrapper | {_addr_link(rep.atlas_wrapper, explorer_url)} |"
            )
        if rep.base_feed:
            feed_text = _addr_link(rep.base_feed, explorer_url)
            if feed_description:
                feed_text += f" — **{feed_description}**"
            pdata_rows.append(f"| Base Chainlink feed | {feed_text} |")
        if rep.median is not None:
            pdata_rows.append(
                f"| Oracle median (raw int192) | `{rep.median}` |"
            )
        if rep.timestamp:
            dt = datetime.datetime.utcfromtimestamp(rep.timestamp)
            pdata_rows.append(
                f"| Oracle observation time | `{rep.timestamp}` "
                f"({dt.strftime('%Y-%m-%d %H:%M:%S UTC')}) |"
            )
        if rep.epoch_and_round:
            pdata_rows.append(f"| Oracle epoch.round | `{rep.epoch_and_round}` |")
        if rep.observations:
            pdata_rows.append(
                f"| # observations / # signatures | "
                f"{len(rep.observations)} / {rep.num_signatures or '?'} |"
            )

    pdata_rows.append(f"| Fork block used by replay | {fork_block} ({fork_source}) |")

    pdata_section = (
        f"## This run's pData\n\n| Field | Value |\n|---|---|\n"
        + "\n".join(pdata_rows)
        + "\n"
    )

    forge_lines: list[str] = []
    if summary.get("forge_status") == "PASS":
        forge_lines.append("- forge: **PASS**")
    elif summary.get("forge_status") == "FAIL":
        phase = summary.get("fail_phase") or "test"
        reason = summary.get("fail_reason") or "(no reason captured)"
        forge_lines.append(f"- forge: **FAIL** in `{phase}()` — {reason}")
    if summary.get("rpc_issue"):
        forge_lines.append(
            "- ⚠️  The forge run looks like it hit a transient RPC issue "
            "(HTTP 5xx / missing trie node / rate limit). The simulation "
            "result below may be unreliable; consider retrying or using a "
            "more stable archive RPC."
        )
    for k, label in (("success", "success"), ("simResult", "simResult"), ("outcome", "outcome")):
        if k in summary:
            forge_lines.append(f"- `{label}`: `{summary[k]}`")
    if summary.get("simResult_decoded"):
        forge_lines.append(f"- simResult decoded: **{summary['simResult_decoded']}**")
    if summary.get("outcome_decoded"):
        bits = ", ".join(summary["outcome_decoded"])
        forge_lines.append(f"- outcome bits set: **{bits}**")

    if not forge_lines:
        forge_lines.append("_(no headline result captured — check the trace file directly)_")

    forge_section = (
        "## Forge replay result (`forge test -vvvv`)\n\n"
        + "\n".join(forge_lines)
        + "\n\n"
        + f"Full trace: see `{trace_relpath}` (addresses pre-labeled with "
        + "`vm.label` so they appear as `[Atlas]`, `[Solver]`, "
        + "`[ChainlinkAtlasWrapper]`, `[<pair> Feed]`, etc.)."
    )

    header = (
        f"# Atlas pData simSolverCall Replay — `{base_name}`\n\n"
        f"I'm debugging an Atlas Protocol pData simulation on **{chain.name} "
        f"({chain.chain_id})**. Use the context below to investigate the trace at "
        f"`{trace_relpath}`.\n"
    )

    body = "\n\n".join(
        [
            header,
            _PROTOCOL_OVERVIEW.rstrip(),
            _ATLAS_REFERENCES.rstrip(),
            chain_section.rstrip(),
            pdata_section.rstrip(),
            forge_section,
            _QUESTIONS.rstrip(),
        ]
    )
    return body + "\n"


def write_prompt(
    *,
    project_root: str,
    base: str,
    pdata: ParsedPData,
    chain: ChainConfig,
    fork_block: int,
    fork_source: str,
    feed_description: str | None,
    pdata_filename: str,
    trace_path: str,
    summary: dict,
) -> str:
    """Render and persist the AI prompt next to the trace log.

    Returns the absolute path of the written file.
    """
    out_dir = os.path.join(project_root, "out")
    os.makedirs(out_dir, exist_ok=True)
    prompt_path = os.path.join(out_dir, f"{base}.prompt.md")

    trace_relpath = os.path.relpath(trace_path, project_root)

    content = build_prompt(
        pdata=pdata,
        chain=chain,
        fork_block=fork_block,
        fork_source=fork_source,
        feed_description=feed_description,
        trace_relpath=trace_relpath,
        pdata_filename=pdata_filename,
        summary=summary,
    )

    Path(prompt_path).write_text(content, encoding="utf-8")
    return prompt_path
