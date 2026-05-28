# Atlas pData Debugger

> **NOTE:** This repo represents an educational example to use a Chainlink system, product, or service and is provided to demonstrate how to interact with Chainlink’s systems, products, and services to integrate them into your own. This template is provided “AS IS” and “AS AVAILABLE” without warranties of any kind, it has not been audited, and it may be missing key checks or error handling to make the usage of the system, product or service more clear. Do not use the code in this example in a production environment without completing your own audits and application of best practices. Neither Chainlink Labs, the Chainlink Foundation, nor Chainlink node operators are responsible for unintended outputs that are generated due to errors in code.

A CLI tool to help Atlas protocol searchers diagnose `simSolverCall` simulation failures.

Given a pData hex file, this tool can:
- **Parse** basic decoded fields (UserOp / SolverOp / DAppOp / Oracle basics) with no RPC-dependent block lookup.
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
# Recommended: save output to a log file for later analysis/sharing.
mkdir -p logs
forge test --match-path test/<pdata_name>.t.sol \
  --match-test test_replay -vvvv \
  --fork-url <RPC_URL> --fork-block-number <YOUR_BLOCK> \
  > logs/replay_<pdata_name>_<YOUR_BLOCK>.log 2>&1

# Step 4 — ask AI to analyze the log and identify the revert root cause.
# In Cursor, open logs/replay_<pdata_name>_<YOUR_BLOCK>.log and ask:
# "Please analyze this Forge replay log and tell me the exact revert reason,
# the failing call path, and suggested fixes."
```

## Commands

### `parse`

Decode-only command. It does:

1. **Decodes the pData hex** offline — UserOp / SolverOp / DAppOp basic fields.
2. **Shows oracle basics** (network chainId, timestamp, wrapper/feed address, epoch/round, signatures, data feed address, median raw value).
3. Does **not** perform block lookups or on-chain scans.
4. See the sample result below:
```
============================================================
  pData Summary
============================================================
  Chain                  Arbitrum (42161)
  Simulator              0x57FA2aBf1dc109C5F7ea2FB6A72358D2c624971d
  Calldata Size          4964 bytes

============================================================
  UserOperation
============================================================
  from                   0xb6065f79d99f29c3eda0ed1bda7ff88e7ee12f1e
  to (Atlas)             0x8ad1ae9d97c79aa68a0a151e83ff3942f68f86c1
  gas                    500000
  maxFeePerGas           30039000 (0.0300 Gwei)
  deadline               459535529
  dapp                   0xe15bba987c002ecc3586e81244517877d294d291
  control                0xe15bba987c002ecc3586e81244517877d294d291
  callConfig             41732
  data selector          0x02a688ed
  data length            1316 bytes

============================================================
  SolverOperation
============================================================
  from (EOA)             0x00003f87cef82f2a4120118a962d956eccfb3cfd
  to (Atlas)             0x8ad1ae9d97c79aa68a0a151e83ff3942f68f86c1
  gas                    6000000
  maxFeePerGas           30039000
  deadline               459535529
  solver contract        0x0e0d47c29cba6dcdbb345bd33e926e6776e4c9ca
  control                0xe15bba987c002ecc3586e81244517877d294d291
  userOpHash             0x170e42536a805779...
  bidToken               0x0000000000000000000000000000000000000000
  bidAmount              82680795878240838
  data selector          0x00000000
  data length            1952 bytes

============================================================
  DAppOperation
============================================================
  bundler                0x9d8a4c00835bfb7bd967c91959a9d21603375140
  deadline               459535529
  userOpHash             0x170e42536a805779...

============================================================
  Oracle Report (basic)
============================================================
  Observation Time       2026-05-05T05:25:05Z (unix: 1777958705)
  Atlas Wrapper          0x9cd5b3e0777b3c85803e6c54c48f905315b9bbe6
  Base Chainlink Feed    0xe7c522c60ba7f1b5e398d2312593713e2b19aeb0
  Epoch & Round          1777958705
  Signatures             4
  Median (raw int192)    8117564124522

  Next step:
    python3 -m atlas_debugger sweep "pdataSample.txt" --rpc <RPC_URL>
```

Try command `python3 -m atlas_debugger parse "pdataSample.txt"` to see the result above. 

### `sweep`
This command owns the block-finding workflow and help you to find the right block number to simulate. 
**Notice**: A userOp does not always land on-chain for multiple reasons: 1) Some assets have duplicate auctions, and it is impossible to know in advance which one will land. If you bid on the losing auction, the solverOp cannot be included on-chain. 2) If an auction does not receive enough valid bids from searchers, none of the userOps in that round can be included on-chain.
**Notice**: The block containing the landed metacall is not always the same block where searcher solverOps were simulated. There can be a gap between the block where Atlas simulated and the block where the metacall landed. This is why a pData may simulate successfully at the landed block but still revert in Atlas. For this reason, the debugger starts simulation from 30 blocks before the landed block.

1. Resolve oracle timestamp to block (if present).
2. Find landed metacall tx (if any). If the userOp landed on-chain: sweep `landed_block - lookback` to `landed_block - 1` (default lookback: 30). If not landed: sweep `oracle_block` to `deadline`.
3. Generate/update `test/<pdata>.t.sol` with the selected fork context.
4. Run `eth_call` simulation per block and print PASS/FAIL quickly.


```bash
# Default behavior (lookback=30 when landed)
python3 -m atlas_debugger sweep "pdataSample.txt" --rpc <RPC_URL>

# Adjust the landed-lookback window
python3 -m atlas_debugger sweep "pdataSample.txt" --rpc <RPC_URL> --lookback 50

# Add delay between blocks if your RPC is rate-limited
python3 -m atlas_debugger sweep "pdataSample.txt" --rpc <RPC_URL> --delay 1.0
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
