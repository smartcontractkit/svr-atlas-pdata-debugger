"""Parse pData from Sorter debug logs into structured operations."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field


@dataclass
class UserOperation:
    from_addr: str
    to_addr: str
    value: int
    gas: int
    max_fee_per_gas: int
    nonce: str
    deadline: int
    dapp: str
    control: str
    call_config: int
    dapp_gas_limit: int
    solver_gas_limit: int
    bundler_surcharge_rate: int
    session_key: str
    data: str


@dataclass
class SolverOperation:
    from_addr: str
    to_addr: str
    value: int
    gas: int
    max_fee_per_gas: int
    deadline: int
    solver: str
    control: str
    user_op_hash: str
    bid_token: str
    bid_amount: int
    data: str
    signature: str


@dataclass
class DAppOperation:
    from_addr: str
    to_addr: str
    nonce: int
    deadline: int
    control: str
    bundler: str
    user_op_hash: str
    call_chain_hash: str
    signature: str


@dataclass
class OracleReport:
    """Decoded Chainlink OCR transmit report embedded in UserOp.data.

    The UserOp.data is nested as:
        update(address wrapper, bytes) -> forward(address baseFeed, bytes) -> transmit(...)
    so we can recover:
      - `atlas_wrapper`: the ChainlinkAtlasWrapper that stores the OEV-captured price
      - `base_feed`: the underlying Chainlink price feed (tells you WHICH asset is being priced)

    The `report` argument of `transmit(bytes report, bytes32[] rs, bytes32[] ss, bytes32 rawVs)`
    is ABI-encoded as (uint32 observationsTimestamp, bytes32 rawObservers, int192[] observations)
    in Chainlink's OffchainAggregator format. The Atlas `ChainlinkAtlasWrapper` decodes the
    first word as `rawReportContext` and tracks `epochAndRound = uint40(uint256(rawReportContext))`.
    """

    timestamp: int  # observationsTimestamp (uint32)
    raw_report_context: str  # 0x-prefixed bytes32 (first word; also what wrapper calls rawReportContext)
    epoch_and_round: int  # low 40 bits of raw_report_context (tracked by ChainlinkAtlasWrapper)
    raw_observers: str  # 0x-prefixed bytes32, low bytes are signer indices
    observer_indices: list[int]  # parsed observer indices (one byte each, stops at >=31)
    observations: list[int]  # signed int192 prices
    median: int | None  # observations[len // 2] — what the wrapper reports
    num_signatures: int | None  # length of rs array
    atlas_wrapper: str | None = None  # ChainlinkAtlasWrapper address (outer `update` arg)
    base_feed: str | None = None  # Underlying Chainlink feed address (inner `forward` arg)
    outer_selector: str | None = None  # e.g. 0x02a688ed (update)
    inner_selector: str | None = None  # e.g. 0x6fadcf72 (forward)


@dataclass
class ParsedPData:
    raw_hex: str
    calldata: str  # with 0x prefix
    simulator: str
    chain_hint: str | None
    gas_fee_cap: int | None
    auction_id: str | None
    solver_from_log: str | None
    result_text: str | None

    user_op: UserOperation | None = None
    solver_op: SolverOperation | None = None
    dapp_op: DAppOperation | None = None
    oracle_timestamp: int | None = None
    oracle_report: OracleReport | None = None

    errors: list[str] = field(default_factory=list)


def _word(words: list[str], idx: int) -> str:
    if idx < len(words):
        return words[idx]
    return "0" * 64


def _addr(word: str) -> str:
    return "0x" + word[-40:]


def _uint(word: str) -> int:
    return int(word, 16)


def _decode_user_op(words: list[str], offset: int) -> UserOperation:
    return UserOperation(
        from_addr=_addr(_word(words, offset)),
        to_addr=_addr(_word(words, offset + 1)),
        value=_uint(_word(words, offset + 2)),
        gas=_uint(_word(words, offset + 3)),
        max_fee_per_gas=_uint(_word(words, offset + 4)),
        nonce="0x" + _word(words, offset + 5),
        deadline=_uint(_word(words, offset + 6)),
        dapp=_addr(_word(words, offset + 7)),
        control=_addr(_word(words, offset + 8)),
        call_config=_uint(_word(words, offset + 9)),
        dapp_gas_limit=_uint(_word(words, offset + 10)),
        solver_gas_limit=_uint(_word(words, offset + 11)),
        bundler_surcharge_rate=_uint(_word(words, offset + 12)),
        session_key=_addr(_word(words, offset + 13)),
        data=_extract_bytes_field(words, offset, 14),
    )


def _decode_solver_op(words: list[str], offset: int) -> SolverOperation:
    return SolverOperation(
        from_addr=_addr(_word(words, offset)),
        to_addr=_addr(_word(words, offset + 1)),
        value=_uint(_word(words, offset + 2)),
        gas=_uint(_word(words, offset + 3)),
        max_fee_per_gas=_uint(_word(words, offset + 4)),
        deadline=_uint(_word(words, offset + 5)),
        solver=_addr(_word(words, offset + 6)),
        control=_addr(_word(words, offset + 7)),
        user_op_hash="0x" + _word(words, offset + 8),
        bid_token=_addr(_word(words, offset + 9)),
        bid_amount=_uint(_word(words, offset + 10)),
        data=_extract_bytes_field(words, offset, 11),
        signature=_extract_bytes_field(words, offset, 12),
    )


def _decode_dapp_op(words: list[str], offset: int) -> DAppOperation:
    return DAppOperation(
        from_addr=_addr(_word(words, offset)),
        to_addr=_addr(_word(words, offset + 1)),
        nonce=_uint(_word(words, offset + 2)),
        deadline=_uint(_word(words, offset + 3)),
        control=_addr(_word(words, offset + 4)),
        bundler=_addr(_word(words, offset + 5)),
        user_op_hash="0x" + _word(words, offset + 6),
        call_chain_hash="0x" + _word(words, offset + 7),
        signature=_extract_bytes_field(words, offset, 8),
    )


def _extract_bytes_field(words: list[str], struct_offset: int, field_idx: int) -> str:
    """Extract a dynamic bytes field from an ABI-encoded struct."""
    try:
        rel_offset = _uint(_word(words, struct_offset + field_idx)) // 32
        data_start = struct_offset + rel_offset
        length = _uint(_word(words, data_start))
        if length == 0:
            return "0x"
        hex_data = ""
        n_words = (length + 31) // 32
        for i in range(data_start + 1, data_start + 1 + n_words):
            hex_data += _word(words, i)
        return "0x" + hex_data[: length * 2]
    except (IndexError, ValueError):
        return "0x"


def _extract_hex_from_text(content: str) -> tuple[str, dict]:
    """Extract the pData hex and metadata from various log formats."""
    metadata: dict = {}

    m = re.search(r'auctionId[=:]\s*["\']?([0-9a-f-]+)', content, re.IGNORECASE)
    if m:
        metadata["auction_id"] = m.group(1)

    m = re.search(r'solverFrom[=:]\s*["\']?(0x[0-9a-fA-F]+)', content, re.IGNORECASE)
    if m:
        metadata["solver_from"] = m.group(1)

    m = re.search(r'simulatorAddr\s+(0x[0-9a-fA-F]+)', content)
    if m:
        metadata["simulator"] = m.group(1)

    m = re.search(r'gasFeeCap\s+(\d+)', content)
    if m:
        metadata["gas_fee_cap"] = int(m.group(1))

    m = re.search(r'result[=:]\s*["\']?(\d+\s*-\s*\w+[^"\']*)', content)
    if m:
        metadata["result_text"] = m.group(1).strip()

    # Try JSON format first
    try:
        j = json.loads(content)
        if isinstance(j, dict):
            res = j.get("result", j)
            if isinstance(res, dict):
                if "auctionId" in res:
                    metadata["auction_id"] = res["auctionId"]
                if "solverOperationFrom" in res:
                    metadata["solver_from"] = res["solverOperationFrom"]
                result_str = res.get("result", "")
                if result_str:
                    metadata["result_text"] = result_str[:120]
                    content = result_str
    except (json.JSONDecodeError, TypeError):
        pass

    # Extract pData hex: look for "pData <hex>," pattern
    m = re.search(r'pData\s+([0-9a-fA-F]{100,})', content)
    if m:
        return m.group(1), metadata

    # Fallback: look for simSolverCall selector b759598a
    m = re.search(r'(b759598a[0-9a-fA-F]{100,})', content)
    if m:
        return m.group(1), metadata

    # Last resort: find the longest hex string
    hexes = re.findall(r'[0-9a-fA-F]{200,}', content)
    if hexes:
        longest = max(hexes, key=len)
        return longest, metadata

    raise ValueError("No pData hex found in input")


CHAINLINK_TRANSMIT_SELECTOR = "ba0cb29e"
# update(address,bytes) — outer wrapper call, address = ChainlinkAtlasWrapper
WRAPPER_UPDATE_SELECTOR = "02a688ed"
# forward(address,bytes) — inner call on wrapper, address = base Chainlink feed
WRAPPER_FORWARD_SELECTOR = "6fadcf72"
# Reasonable timestamp range: 2024-01-01 to 2030-01-01
_TS_MIN = 1704067200
_TS_MAX = 1893456000


def _peel_address_bytes_call(calldata_hex: str, expected_selector: str) -> tuple[str, str] | None:
    """Decode a `fn(address, bytes)` call and return (address, inner_calldata_hex).

    `calldata_hex` is a hex string without 0x prefix. Returns None if selector mismatches
    or decoding fails.
    """
    if len(calldata_hex) < 8 + 64 * 3:
        return None
    if calldata_hex[:8].lower() != expected_selector.lower():
        return None
    try:
        addr = "0x" + calldata_hex[8 + 24 : 8 + 64]
        bytes_offset = int(calldata_hex[8 + 64 : 8 + 128], 16) * 2  # words -> hex chars since 32B = 64 hex
        # offset is in bytes from start of args; args start at offset 8 in our slice
        head_end = 8 + bytes_offset  # = end of the 2-word head for fn(address,bytes)
        length_hex = calldata_hex[head_end : head_end + 64]
        length = int(length_hex, 16)
        inner = calldata_hex[head_end + 64 : head_end + 64 + length * 2]
        return addr, inner
    except (ValueError, IndexError):
        return None


def _signed_int192(word_hex: str) -> int:
    """Decode the low 24 bytes of a 32-byte word as a signed int192."""
    val = int(word_hex[-48:], 16)
    if val >= (1 << 191):
        val -= (1 << 192)
    return val


def _parse_observer_indices(raw_observers_hex: str, count: int | None = None) -> list[int]:
    """rawObservers is 32 bytes; the first N bytes are the oracle indices used.

    If `count` is provided (typically the observation count), we take exactly that many
    bytes. Otherwise, we heuristically stop at the first byte >= MAX_NUM_ORACLES (31),
    since unused slots are zero-padded and 0 is a valid oracle index.
    """
    indices: list[int] = []
    if count is not None:
        for i in range(count):
            off = i * 2
            if off + 2 > len(raw_observers_hex):
                break
            indices.append(int(raw_observers_hex[off : off + 2], 16))
        return indices

    for i in range(0, len(raw_observers_hex), 2):
        b = int(raw_observers_hex[i : i + 2], 16)
        if b >= 31:
            break
        indices.append(b)
    return indices


def _extract_oracle_report(uo_data_hex: str) -> "OracleReport | None":
    """Extract the Chainlink OCR report embedded in UserOp.data.

    The data nesting is typically:
      02a688ed -> 6fadcf72 -> ba0cb29e(reportContext[3], report, rs, ss, rawVs)
    The report bytes are ABI-encoded as (bytes32 rawReportContext, bytes32 rawObservers, int192[] observations)
    where the first word also contains observationsTimestamp in its low 4 bytes
    (this is how Chainlink OffchainAggregator parses it).
    """
    data = uo_data_hex
    if data.startswith("0x"):
        data = data[2:]

    atlas_wrapper: str | None = None
    base_feed: str | None = None
    outer_selector: str | None = None
    inner_selector: str | None = None

    # Peel outer update(address wrapper, bytes) -> inner forward(address feed, bytes transmit)
    peeled_outer = _peel_address_bytes_call(data, WRAPPER_UPDATE_SELECTOR)
    if peeled_outer:
        atlas_wrapper, inner_call = peeled_outer
        outer_selector = "0x" + WRAPPER_UPDATE_SELECTOR
        peeled_inner = _peel_address_bytes_call(inner_call, WRAPPER_FORWARD_SELECTOR)
        if peeled_inner:
            base_feed, _innermost = peeled_inner
            inner_selector = "0x" + WRAPPER_FORWARD_SELECTOR

    pos = data.find(CHAINLINK_TRANSMIT_SELECTOR)
    if pos < 0:
        return None

    params_start = pos + 8
    remaining = data[params_start:]
    if len(remaining) < 64 * 9:
        return None

    words = [remaining[i * 64 : (i + 1) * 64] for i in range(len(remaining) // 64)]

    try:
        report_byte_offset = _uint(words[3]) // 32
        rs_byte_offset = _uint(words[4]) // 32
        report_word_idx = report_byte_offset

        # ABI-encoded bytes: [length] [content...]
        # Content layout observed on-chain (Chainlink OffchainAggregator):
        #   [0] observationsTimestamp (uint32, left-padded to 32 bytes)
        #   [1] rawObservers
        #   [2] offset to int192[] observations (relative to start of content)
        #   [3] extra uint256 (juelsPerFeeCoin or similar; ignored by Atlas wrapper)
        #   [offset/32] length of observations
        #   [offset/32 + 1..] observations
        # Some variants use only 3 head slots -> offset = 0x60. Following the
        # offset handles both layouts uniformly.
        content_start = report_word_idx + 1
        raw_report_context = words[content_start]
        timestamp = int(raw_report_context, 16)
        if not (_TS_MIN <= timestamp <= _TS_MAX):
            timestamp = 0

        raw_observers = words[content_start + 1]
        obs_offset_words = _uint(words[content_start + 2]) // 32
        obs_length_idx = content_start + obs_offset_words
        obs_len = _uint(words[obs_length_idx])

        observations: list[int] = []
        if 0 < obs_len < 64:
            for i in range(obs_len):
                observations.append(_signed_int192(words[obs_length_idx + 1 + i]))

        num_sigs = None
        try:
            num_sigs = _uint(words[rs_byte_offset])
        except (IndexError, ValueError):
            pass

        context_int = int(raw_report_context, 16)
        epoch_and_round = context_int & ((1 << 40) - 1)

        median = observations[len(observations) // 2] if observations else None

        if timestamp == 0 and not observations:
            return None

        return OracleReport(
            timestamp=timestamp,
            raw_report_context="0x" + raw_report_context,
            epoch_and_round=epoch_and_round,
            raw_observers="0x" + raw_observers,
            observer_indices=_parse_observer_indices(raw_observers, count=len(observations) or None),
            observations=observations,
            median=median,
            num_signatures=num_sigs,
            atlas_wrapper=atlas_wrapper,
            base_feed=base_feed,
            outer_selector=outer_selector,
            inner_selector=inner_selector,
        )
    except (IndexError, ValueError):
        return None


def _extract_oracle_timestamp(uo_data_hex: str) -> int | None:
    """Backwards-compatible helper that returns only the timestamp."""
    report = _extract_oracle_report(uo_data_hex)
    if report and report.timestamp:
        return report.timestamp

    data = uo_data_hex[2:] if uo_data_hex.startswith("0x") else uo_data_hex
    pos = data.find(CHAINLINK_TRANSMIT_SELECTOR)
    if pos < 0:
        return None
    remaining = data[pos + 8 :]
    words = [remaining[i * 64 : (i + 1) * 64] for i in range(len(remaining) // 64)]
    for w in words:
        try:
            val = int(w, 16)
            if _TS_MIN <= val <= _TS_MAX:
                return val
        except ValueError:
            continue
    return None


def parse_pdata(file_path: str) -> ParsedPData:
    """Parse a pData file and return structured data."""
    with open(file_path, "r") as f:
        content = f.read()

    raw_hex, metadata = _extract_hex_from_text(content)

    # Ensure it starts with the simSolverCall selector
    if not raw_hex.startswith("b759598a"):
        raise ValueError(
            f"pData doesn't start with simSolverCall selector (b759598a), got: {raw_hex[:8]}"
        )

    # Split into 32-byte words (skip 4-byte selector)
    data = raw_hex[8:]
    words = [data[i : i + 64] for i in range(0, len(data), 64)]

    errors: list[str] = []

    # Decode offsets for the three structs
    uo_offset = _uint(words[0]) // 32
    so_offset = _uint(words[1]) // 32
    do_offset = _uint(words[2]) // 32

    user_op = None
    solver_op = None
    dapp_op = None

    try:
        user_op = _decode_user_op(words, uo_offset)
    except Exception as e:
        errors.append(f"Failed to decode UserOperation: {e}")

    try:
        solver_op = _decode_solver_op(words, so_offset)
    except Exception as e:
        errors.append(f"Failed to decode SolverOperation: {e}")

    try:
        dapp_op = _decode_dapp_op(words, do_offset)
    except Exception as e:
        errors.append(f"Failed to decode DAppOperation: {e}")

    oracle_ts = None
    oracle_report = None
    if user_op and user_op.data:
        try:
            oracle_report = _extract_oracle_report(user_op.data)
            if oracle_report:
                oracle_ts = oracle_report.timestamp or None
            else:
                oracle_ts = _extract_oracle_timestamp(user_op.data)
        except Exception:
            pass

    return ParsedPData(
        raw_hex=raw_hex,
        calldata="0x" + raw_hex,
        simulator=metadata.get("simulator", ""),
        chain_hint=None,
        gas_fee_cap=metadata.get("gas_fee_cap"),
        auction_id=metadata.get("auction_id"),
        solver_from_log=metadata.get("solver_from"),
        result_text=metadata.get("result_text"),
        user_op=user_op,
        solver_op=solver_op,
        dapp_op=dapp_op,
        oracle_timestamp=oracle_ts,
        oracle_report=oracle_report,
        errors=errors,
    )
