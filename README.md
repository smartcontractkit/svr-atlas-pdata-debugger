# Atlas pData Debugger

A CLI tool to help Atlas protocol searchers diagnose `simSolverCall` simulation failures.

Given a pData hex file, this tool can:
- **Parse** all fields (UserOp, SolverOp, DAppOp), the embedded Chainlink OCR `transmit` report (every observation + median), and the underlying base feed (e.g. `BTC / USD`).
- **Find the on-chain landed metacall tx** for the auction (works even when a different solver won the bid).
- **Generate** a standalone Foundry test (`test/<pdata_name>.t.sol`) at the right fork block, with all known addresses pre-labeled via `vm.label` so the trace shows readable names (`[Atlas]`, `[Solver]`, `[ChainlinkAtlasWrapper]`, `[BTC / USD Feed]`, …).
- **(opt-in) Run `forge test -vvvv` automatically** and produce two artifacts in `out/`:
  - a clean (ANSI-stripped) `<pdata_name>.trace.log`,
  - an AI-ready `<pdata_name>.prompt.md` with Atlas protocol context, **chain-specific config** (per Arbitrum / Base / Ethereum / BSC, with the right block-explorer URLs and quirks), parsed pData summary, decoded `simResult`/`outcome`, source-code links, and a checklist of questions for the AI to answer.

## Requirements

- Python 3.10+
- [Foundry](https://book.getfoundry.sh/) (`cast` and `forge` must be in PATH)
- An RPC endpoint for the target chain (built-in free archive RPCs are used by default; provide your own via `--rpc` for reliability)

## Quick Start

Get the pData for your simulation. See how to query a bid
[here](https://docs.chain.link/data-feeds/svr-feeds/searcher-onboarding-atlas#tracing-solver-operation-results).
If you hit a "solverop not found" issue, use the
[test bot](https://github.com/QingyangKong/test-bot) to verify the if way you send your request is correct.

Save your `pData.txt` in this directory, then pick the workflow that matches
how much manual control you want over the fork block:

### Mode A — One-shot

Let `--auto-trace` do everything: parse → find-tx → generate the `.t.sol` →
fork at the auto-picked block (`landed_block - 1`, or `oracle_block - 1` if
the auction never landed) → run `forge test -vvvv` → save the trace and an
AI-ready prompt.

```bash
cd atlas-pdata-debugger
python3 -m atlas_debugger parse <pdata_file> --rpc <RPC_URL> --auto-trace
```

This produces three files:

| Path | Purpose |
|---|---|
| `test/<pdata_name>.t.sol` | The Foundry replay test (with `vm.label` for every known address). |
| `out/<pdata_name>.trace.log` | Clean (ANSI-stripped) `forge test -vvvv` output. |
| `out/<pdata_name>.prompt.md` | AI prompt bundling Atlas overview + per-chain config + pData summary + decoded `simResult`/`outcome` + source-code links. |

In Cursor (or any AI chat that accepts file attachments), reference both
`out/<pdata_name>.prompt.md` and `out/<pdata_name>.trace.log` and the AI has
all the context it needs to root-cause the failure.

### Mode B — Two-step (manual fork-block control)

Use this when you want to **pick the fork block yourself** — for example to
sweep blocks around the deadline, replay at a historical block before the
oracle update, or skip the (sometimes slow) forge run while you only need
the parsed pData summary.

```bash
cd atlas-pdata-debugger

# Step 1 — parse only. Prints the pData summary, the recommended fork block
# at the end, and a ready-to-copy `forge test` command (with --match-path
# already pointing at the file just generated under test/).
python3 -m atlas_debugger parse <pdata_file> --rpc <RPC_URL>

# Step 2 — run forge test yourself, choosing whichever fork block you want
# (the one printed by Step 1, or any other historical block you want to
# debug at). Replace <RPC_URL> with an archive RPC.
forge test --match-path test/<pdata_name>.t.sol \
  --match-test test_replay -vvvv \
  --fork-url <RPC_URL> --fork-block-number <YOUR_BLOCK>
```

The Step-2 command is also printed verbatim at the end of Step 1 with the
recommended block already filled in, so you can copy it and only edit the
`--fork-block-number` if you want a different block.

> **Why `--match-path`?** Each `parse` run drops a new `<pdata_name>.t.sol`
> into `test/`, so without scoping forge would re-execute every prior replay
> test in the directory. `--match-path` keeps the run focused on the file
> you just generated.

> **Mode A vs Mode B.** Mode A is the fastest path to an AI-ready report,
> but the fork block is auto-picked (`landed_block - 1` if the auction
> landed on-chain, otherwise `oracle_block - 1`). Mode B is preferred when
> you want to iterate on the fork block (e.g. sweep blocks around the
> deadline, or replay at a block before the oracle update), or when forge
> is too slow / your RPC is flaky and you want to retry the forge step by
> hand. Mode B does **not** generate `out/*.trace.log` or
> `out/*.prompt.md`; you can still feed an AI the trace by pasting forge's
> stdout, but you lose the auto-built prompt context.

## Commands

### `parse`

The main command. Does **everything** end-to-end:

1. **Decodes the pData hex** offline — UserOp / SolverOp / DAppOp struct fields, deadline, bid, gas params
2. **Decodes the embedded Chainlink `transmit` report** — `observationsTimestamp`, `rawReportContext`, epoch & round, `rawObservers`, observer indices, signature count, and every `int192` observation (with the median highlighted — this is the value `ChainlinkAtlasWrapper.transmit` writes)
3. **Recovers the `ChainlinkAtlasWrapper` and underlying base feed** (e.g. which asset's price is being updated), then calls `description()` and `decimals()` on the feed via RPC to show a human-readable pair like `BTC / USD`
4. **Resolves `observationsTimestamp` to a block number**
5. **Runs `find-tx`** to locate the on-chain Atlas metacall transaction that actually landed for this pData (filters `MetacallResult` logs by `user = UserOp.from`, then matches `userOpHash` inside the tx calldata — works regardless of which solver won)
6. **Auto-generates a Foundry replay test** at `landed_block - 1` (or `oracle_block - 1` if no landing tx was found), written to `test/<pdata_name>.t.sol`, ready to run with `forge test -vvvv`. The generated test pre-registers `vm.label(...)` calls for every known address (Atlas, Solver, DAppControl, ChainlinkAtlasWrapper, base feed with its asset pair, etc.) so the verbose trace shows readable names like `[ChainlinkAtlasWrapper]::update(...)` instead of bare `0x…` addresses.
7. **(opt-in) Auto-runs `forge test -vvvv`** under `--auto-trace`: forks the right archive RPC at the right block, streams forge's output to your terminal, and saves **two** artifacts to `out/`:
   - `out/<pdata_name>.trace.log` — clean (ANSI-stripped) forge trace.
   - `out/<pdata_name>.prompt.md` — an AI-ready Markdown prompt that bundles the Atlas protocol overview, the per-chain configuration (explorer URLs and quirks vary by Arbitrum / Base / Ethereum / BSC), the parsed pData summary, the forge result headline, **links to the Atlas source code** on GitHub, and a checklist of concrete questions for the AI to answer.

   In Cursor or any chat-with-attachments AI, drop the prompt file (and the trace) into the conversation and the AI has every layer of context it needs — no manual copy-paste of addresses, enums or protocol explanations.

   The summary at the end distinguishes a real on-chain failure from a flaky-RPC failure (HTTP 5xx, missing trie node, rate limit) so you don't chase a phantom bug.

```bash
python3 -m atlas_debugger parse "sample pData.txt"

# Use a custom RPC (recommended: speeds up oracle block + find-tx + feed lookups)
python3 -m atlas_debugger parse "sample pData.txt" --rpc <RPC_URL>

# Auto-run forge test -vvvv after generation; trace saved to
# out/sample.trace.log, ready to paste into AI alongside the printed summary
python3 -m atlas_debugger parse "sample pData.txt" --rpc <RPC_URL> --auto-trace

# Skip the on-chain find-tx step (offline-only; no eth_getLogs calls)
python3 -m atlas_debugger parse "sample pData.txt" --no-find-tx

# Skip Foundry test generation
python3 -m atlas_debugger parse "sample pData.txt" --no-generate

# Widen the find-tx scan window
python3 -m atlas_debugger parse "sample pData.txt" --before 20 --after 30
```

### `find-tx`

Locates the **on-chain Atlas `metacall` transaction** that actually landed for a given pData — even if a different solver won the auction.

How it works:

1. Filters `MetacallResult(bundler, user, …)` logs on the Atlas contract (`= UserOp.to`), indexed by `user = UserOp.from`, across the block range `[oracle_block − before, deadline + after]`.
2. For each candidate, fetches the tx calldata and matches our `userOpHash` inside it (since the userOp/dAppOp in the metacall calldata contains the same hash).
3. Reports the block height, tx hash, bundler, solver success flag and ETH paid.

```bash
python3 -m atlas_debugger find-tx "sample pData.txt"

# Use a custom RPC (must support eth_getLogs over the range; no debug_ required)
python3 -m atlas_debugger find-tx "sample pData.txt" --rpc <RPC_URL>

# Widen the scan window
python3 -m atlas_debugger find-tx "sample pData.txt" --before 20 --after 30
```

Example output:

```
Landing Transaction (1 match)
  Block                  448889822
  Tx Hash                0xd6acc1cd85f3926bc8ae5ec5eef80c82a8fb33fbdbeec05d1542217625c21be7
  Bundler                0xbdaf054a42a32e7fbc4ef094f6121b8a84410d92
  Solver Successful      True
  ETH Paid to Bundler    7860696844000 wei (0.0000078607 ETH)
```

### `generate`

Standalone Foundry test generator. `parse` already runs this at the end, so you usually don't need to call it directly. Useful when you want a custom output path or have already parsed a pData and just need a fresh `.t.sol`.

- Output defaults to the project's `test/` directory.
- Automatically includes Arbitrum precompile mocks (`arbBlockNumber`, `getPricesInArbGas`) when targeting Arbitrum.
- Embeds the correct `vm.txGasPrice`, simulator address, and `vm.label(...)` calls for every known address.
- Fork block defaults to `oracle_block` (resolved from the embedded oracle timestamp via RPC) or `deadline - 100` if no oracle timestamp is present. Use `--rpc` to enable the timestamp lookup.

```bash
python3 -m atlas_debugger generate "sample pData.txt"
python3 -m atlas_debugger generate "sample pData.txt" --rpc <RPC_URL>
python3 -m atlas_debugger generate "sample pData.txt" -o my_test.t.sol
```

## Supported Chains

| Chain    | Chain ID | Auto-detected | Arb Precompile Mocks |
|----------|----------|---------------|----------------------|
| Ethereum | 1        | Yes           | No                   |
| BSC      | 56       | Yes           | No                   |
| Base     | 8453     | Yes           | No                   |
| Arbitrum | 42161    | Yes           | Yes                  |

## pData File Format

The tool accepts files containing the pData hex in several formats:
- Raw hex starting with `b759598a` (the `simSolverCall` selector)
- Sorter debug log lines containing `pData <hex>`
- JSON with the pData embedded

## Architecture

```
atlas-pdata-debugger/
├── foundry.toml              # Foundry config (for generated tests)
├── lib/forge-std/            # Forge standard library (used by replay tests)
├── test/                     # Generated <pdata>.t.sol files go here
├── out/                      # Foundry build dir + auto-trace artifacts:
│                             #   <pdata>.trace.log, <pdata>.prompt.md
├── pyproject.toml            # Python package metadata
├── README.md
└── atlas_debugger/
    ├── __init__.py
    ├── __main__.py           # python3 -m atlas_debugger entry
    ├── cli.py                # CLI argument parsing, command dispatch, --auto-trace driver
    ├── parser.py             # pData hex → UserOp/SolverOp/DAppOp + OracleReport
    ├── chain.py              # Auto-detect chain from contract addresses
    ├── constants.py          # Chain configs, result codes, known error selectors
    ├── rpc.py                # Multi-RPC failover (cast + JSON-RPC) + feed metadata lookup
    ├── find_tx.py            # Locate the on-chain landed metacall via MetacallResult logs
    ├── forge_gen.py          # Foundry replay test (.t.sol) generator with vm.label support
    ├── simulator.py          # eth_call-based simSolverCall simulation
    ├── tracer.py             # debug_traceCall + CallFrame tree + revert analysis
    ├── foundry_tracer.py     # Fallback tracer using `forge test -vvvv`
    ├── analyzer.py           # Rule-based diagnoses on top of trace results
    └── report.py             # Builds the AI prompt (out/<pdata>.prompt.md) — chain-aware
```
