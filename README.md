# Atlas pData Debugger

> **NOTE:** This repo represents an educational example to use a Chainlink system, product, or service and is provided to demonstrate how to interact with Chainlink’s systems, products, and services to integrate them into your own. This template is provided “AS IS” and “AS AVAILABLE” without warranties of any kind, it has not been audited, and it may be missing key checks or error handling to make the usage of the system, product or service more clear. Do not use the code in this example in a production environment without completing your own audits and application of best practices. Neither Chainlink Labs, the Chainlink Foundation, nor Chainlink node operators are responsible for unintended outputs that are generated due to errors in code.

A CLI tool to help Atlas protocol searchers diagnose `simSolverCall` simulation failures.

Given a pData hex file, this tool can:
- **Parse** basic decoded fields (UserOp / SolverOp / DAppOp / Oracle basics) with no RPC-dependent block lookup.
- **Find the on-chain landed metacall tx** for the auction (works even when a different solver won the bid).
- **Generate** a standalone Foundry test (`test/<pdata_name>.t.sol`) at the right fork block, with all known addresses pre-labeled via `vm.label`.
- **Sweep execution across blocks** (in `sweep`) to find pass/fail boundaries before choosing a detailed replay block.

## Requirements

- Python 3.10+
- [Foundry](https://book.getfoundry.sh/) (`cast` and `forge` must be in PATH)
- An RPC endpoint for the target chain (built-in free archive RPCs are used by default; provide your own via `--rpc` for reliability)

## Quick Start

Get the pData for your simulation. See how to query a bid
[here](https://docs.chain.link/data-feeds/svr-feeds/searcher-onboarding-atlas#tracing-solver-operation-results).
If you hit a "solverop not found" issue, use the
[test bot](https://github.com/QingyangKong/test-bot) to verify the if way you send your request is correct.

Save your `pData.txt` in this directory, then use this manual workflow:

```bash
cd atlas-pdata-debugger

# Step 1 — parse: decode pData basics only.
# no block lookup is done in this step.
python3 -m atlas_debugger parse <pdata_file>

# Step 2 — sweep: resolves block numbers, generates test/<pdata>.t.sol, then
# uses eth_call to quickly scan multiple blocks and print
# PASS/FAIL per block so you can find the boundary.
python3 -m atlas_debugger sweep <pdata_file> --rpc <RPC_URL>

# If landed metacall is NOT FOUND, you may be querying a dropped auction.
# In Chainlink SVR, some assets can have duplicate auctions and which one
# lands cannot be known in advance, so you should submit solverOps for both
# duplicate auctions.

# Step 3 — pick the block you care about and run full verbose replay.
forge test --match-path test/<pdata_name>.t.sol \
  --match-test test_replay -vvvv \
  --fork-url <RPC_URL> --fork-block-number <YOUR_BLOCK>
```

`sweep` block-selection logic:

1. If the userOp **landed on-chain**, sweep the **30 blocks before** the landed block (`landed_block - 30` to `landed_block - 1` by default).
2. If the userOp **did not land**, sweep the range from **oracle timestamp block** to **deadline block**.

> `--match-path` keeps forge focused on the generated test file so you don't
> run every old replay file under `test/`.

## Commands

### `parse`

Decode-only command. It does:

1. **Decodes the pData hex** offline — UserOp / SolverOp / DAppOp basic fields.
2. **Shows oracle basics** (timestamp, wrapper/feed address, epoch/round, signatures, median raw value).
3. Does **not** perform block lookups or on-chain scans.

```bash
python3 -m atlas_debugger parse "pdataSample.txt"
```

### `sweep`

This command owns the block-finding workflow:

1. Resolve oracle timestamp to block (if present).
2. Find landed metacall tx (if any).
3. Generate/update `test/<pdata>.t.sol` with the selected fork context.
4. Run `eth_call` simulation per block and print PASS/FAIL quickly.

Selection logic:

1. If the userOp landed on-chain: sweep `landed_block - lookback` to `landed_block - 1` (default lookback: 30).
2. If not landed: sweep `oracle_block` to `deadline`.

```bash
# Default behavior (lookback=30 when landed)
python3 -m atlas_debugger sweep "pdataSample.txt" --rpc <RPC_URL>

# Adjust the landed-lookback window
python3 -m atlas_debugger sweep "pdataSample.txt" --rpc <RPC_URL> --lookback 50

# Add delay between blocks if your RPC is rate-limited
python3 -m atlas_debugger sweep "pdataSample.txt" --rpc <RPC_URL> --delay 1.0
```

### `find-tx`

Locates the **on-chain Atlas `metacall` transaction** that actually landed for a given pData — even if a different solver won the auction.

How it works:

1. Filters `MetacallResult(bundler, user, …)` logs on the Atlas contract (`= UserOp.to`), indexed by `user = UserOp.from`, across the block range `[oracle_block − before, deadline + after]`.
2. For each candidate, fetches the tx calldata and matches our `userOpHash` inside it (since the userOp/dAppOp in the metacall calldata contains the same hash).
3. Reports the block height, tx hash, bundler, solver success flag and ETH paid.

```bash
python3 -m atlas_debugger find-tx "pdataSample.txt"

# Use a custom RPC (must support eth_getLogs over the range; no debug_ required)
python3 -m atlas_debugger find-tx "pdataSample.txt" --rpc <RPC_URL>

# Widen the scan window
python3 -m atlas_debugger find-tx "pdataSample.txt" --before 20 --after 30
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

Standalone Foundry test generator. Useful when you want to generate/update `test/<pdata>.t.sol` directly without running `sweep`.

- Output defaults to the project's `test/` directory.
- Automatically includes Arbitrum precompile mocks (`arbBlockNumber`, `getPricesInArbGas`) when targeting Arbitrum.
- Embeds the correct `vm.txGasPrice`, simulator address, and `vm.label(...)` calls for every known address.
- Fork block defaults to `oracle_block` (resolved from the embedded oracle timestamp via RPC) or `deadline - 100` if no oracle timestamp is present. Use `--rpc` to enable the timestamp lookup.

```bash
python3 -m atlas_debugger generate "pdataSample.txt"
python3 -m atlas_debugger generate "pdataSample.txt" --rpc <RPC_URL>
python3 -m atlas_debugger generate "pdataSample.txt" -o my_test.t.sol
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
├── out/                      # Foundry build output directory
├── pyproject.toml            # Python package metadata
├── README.md
└── atlas_debugger/
    ├── __init__.py
    ├── __main__.py           # python3 -m atlas_debugger entry
    ├── cli.py                # CLI argument parsing and command dispatch
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
    └── report.py             # AI prompt builder utility (optional)
```
