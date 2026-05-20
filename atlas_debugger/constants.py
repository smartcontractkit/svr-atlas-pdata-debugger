"""Chain configurations and known contract addresses."""

from dataclasses import dataclass


@dataclass
class ChainConfig:
    name: str
    chain_id: int
    rpcs: list[str]
    atlas: str
    simulator: str
    block_time_sec: float
    arb_precompiles: bool = False


CHAINS: dict[str, ChainConfig] = {
    "bsc": ChainConfig(
        name="BSC",
        chain_id=56,
        rpcs=["https://bsc-dataseed.binance.org"],
        atlas="0x21B7d28B882772A1Cfe633Daee6f42ebb95DeC4E",
        simulator="0x7a28f2c7310454C3440C7b36d59B21aA354b446f",
        block_time_sec=3.0,
    ),
    "base": ChainConfig(
        name="Base",
        chain_id=8453,
        rpcs=["https://mainnet.base.org"],
        atlas="0x583dCfEF0d240Dc80753f0f0b26513FEe27D9B77",
        simulator="0x3BF81d7D921E7a6A1999ce3dfa3B348c50fE8DFd",
        block_time_sec=2.0,
    ),
    "arbitrum": ChainConfig(
        name="Arbitrum",
        chain_id=42161,
        rpcs=["https://arb1.arbitrum.io/rpc", "https://arbitrum.drpc.org"],
        atlas="0x8ad1aE9D97C79aA68A0a151E83ff3942f68F86C1",
        simulator="0x57FA2aBf1dc109C5F7ea2FB6A72358D2c624971d",
        block_time_sec=0.25,
        arb_precompiles=True,
    ),
    "ethereum": ChainConfig(
        name="Ethereum",
        chain_id=1,
        rpcs=["https://eth.drpc.org"],
        atlas="0x38eE462B37793e5e62b6bC1a5F5f1be9fBF3e26d",
        simulator="0x1244E84965e3F4b3e282c2f180b399dB17948f83",
        block_time_sec=12.0,
    ),
}

RESULT_NAMES = {
    0: "Unknown",
    1: "VerificationSimFail",
    2: "PreOpsSimFail",
    3: "UserOpSimFail",
    4: "SolverSimFail",
    5: "AllocateValueSimFail",
    6: "SimulationPassed",
}

VERIFICATION_FAIL_CODES = {
    0: "ValidCallsResult(0) - Unknown",
    1: "InvalidTo",
    2: "InvalidDAppControl",
    3: "InvertBidValueCannotBeExPostBids",
    4: "UserNonceInvalid",
    5: "InvalidDAppNonce",
    6: "InvalidBundler",
    7: "InvertedBidExceedsCeiling",
    8: "InvalidSolverCount",
    9: "CallConfigConflict",
    10: "GasPriceHigherThanMax",
    11: "TxValueLowerThanCallValue",
    12: "TooManySolverOps",
    13: "UserDeadlineReached",
    14: "SolverDeadlineReached",
    15: "InvalidSignature",
    16: "DAppNotEnabled",
    17: "NeedPreOps",
}

SOLVER_OUTCOME_BITS = {
    0: "InvalidTo",
    1: "InvalidSolver",
    2: "InvalidUserHash",
    3: "InvalidControlHash",
    4: "InvalidBidsHash",
    5: "InvalidSequencing",
    6: "GasPriceOverCap",
    7: "UserOutOfGas",
    8: "InsufficientEscrow",
    9: "InvalidNonceOver",
    10: "AlreadyExecuted",
    11: "InvalidNonceUnder",
    12: "PerBlockLimit",
    13: "InvalidFormat",
    14: "UnusedBit14",
    15: "UnusedBit15",
    16: "LostAuction",
    17: "SolverOpReverted",
    18: "PreSolverFailed",
    19: "BidNotPaid",
    20: "BalanceNotReconciled",
    21: "CallbackFailed",
    22: "EVMError",
}

KNOWN_ERROR_SELECTORS = {
    "f803a2ca": "StaleReport()",
    "aa2d4fb6": "OracleUpdateFailed()",
    "d5ee2640": "SolverOpReverted()",
    "08c379a0": "Error(string)",
    "4e487b71": "Panic(uint256)",
}
