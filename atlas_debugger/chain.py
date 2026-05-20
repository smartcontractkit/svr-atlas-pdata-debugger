"""Auto-detect chain from pData addresses."""

from __future__ import annotations

from .constants import CHAINS, ChainConfig
from .parser import ParsedPData


def detect_chain(pdata: ParsedPData) -> ChainConfig:
    """Detect the chain based on Atlas/Simulator/DAppControl addresses in the pData."""
    candidates: list[str] = []

    atlas_addr = ""
    if pdata.user_op:
        atlas_addr = pdata.user_op.to_addr.lower()

    simulator_addr = pdata.simulator.lower() if pdata.simulator else ""

    for key, cfg in CHAINS.items():
        if atlas_addr and atlas_addr == cfg.atlas.lower():
            candidates.append(key)
            continue
        if simulator_addr and simulator_addr == cfg.simulator.lower():
            candidates.append(key)
            continue

    if not candidates:
        raise ValueError(
            f"Cannot detect chain. Atlas={atlas_addr}, Simulator={simulator_addr}. "
            f"Known chains: {list(CHAINS.keys())}"
        )

    chain_key = candidates[0]
    return CHAINS[chain_key]
