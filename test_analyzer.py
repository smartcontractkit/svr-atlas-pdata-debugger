"""Test the tracer + analyzer pipeline with mock trace data.

Simulates the known trace results from our previous debugging sessions:
- pdata0404: SolverOpReverted due to "ERC20: burn amount exceeds balance" in WETH.withdraw()
- pdata0402: UserOpSimFail due to StaleReport() in Chainlink oracle
"""

from atlas_debugger.tracer import CallFrame, TraceResult, find_all_reverts, find_deepest_revert, print_call_tree
from atlas_debugger.analyzer import analyze_trace, format_diagnoses


def _build_0404_trace() -> TraceResult:
    """Reconstruct the trace structure from pdata0404 analysis.
    Root cause: WETH.withdraw() reverts with "ERC20: burn amount exceeds balance"
    """
    # Error(string) encoding for "ERC20: burn amount exceeds balance"
    error_string = "ERC20: burn amount exceeds balance"
    encoded_str = error_string.encode("utf-8").hex()
    padded_len = ((len(encoded_str) // 2 + 31) // 32) * 64
    error_output = (
        "0x08c379a0"
        + "0000000000000000000000000000000000000000000000000000000000000020"
        + f"{len(error_string):064x}"
        + encoded_str.ljust(padded_len, "0")
    )

    # Deepest revert: WETH.withdraw() -> ERC20: burn amount exceeds balance
    weth_burn = CallFrame(
        call_type="CALL",
        from_addr="0xf7527ada8c19796d9db0fc629a9895d699470628",
        to_addr="0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
        input_data="0x2e1a7d4d000000000000000000000000000000000000000000000000002386f26fc10000",
        output_data=error_output,
        gas=500000,
        gas_used=3200,
        value=0,
        error="execution reverted",
        depth=5,
    )

    # Solver contract call that contains the WETH withdrawal
    solver_call = CallFrame(
        call_type="DELEGATECALL",
        from_addr="0x8ad1ae9d97c79aa68a0a151e83ff3942f68f86c1",
        to_addr="0xf7527ada8c19796d9db0fc629a9895d699470628",
        input_data="0xa8bfa466",
        output_data="0x",
        gas=5000000,
        gas_used=50000,
        value=0,
        error="execution reverted",
        depth=4,
        calls=[weth_burn],
    )

    # Atlas executeSolverOp
    atlas_exec = CallFrame(
        call_type="CALL",
        from_addr="0x57fa2abf1dc109c5f7ea2fb6a72358d2c624971d",
        to_addr="0x8ad1ae9d97c79aa68a0a151e83ff3942f68f86c1",
        input_data="0xb759598a",
        output_data="0x",
        gas=6000000,
        gas_used=200000,
        value=0,
        depth=1,
        calls=[
            CallFrame(
                call_type="CALL",
                from_addr="0x8ad1ae9d97c79aa68a0a151e83ff3942f68f86c1",
                to_addr="0xe15bba987c002ecc3586e81244517877d294d291",
                input_data="0x02a688ed",
                output_data="0x",
                gas=500000,
                gas_used=100000,
                value=0,
                depth=2,
            ),
            CallFrame(
                call_type="CALL",
                from_addr="0x8ad1ae9d97c79aa68a0a151e83ff3942f68f86c1",
                to_addr="0x8ad1ae9d97c79aa68a0a151e83ff3942f68f86c1",
                input_data="0x12345678",
                output_data="0x",
                gas=5500000,
                gas_used=60000,
                value=0,
                error="execution reverted",
                depth=3,
                calls=[solver_call],
            ),
        ],
    )

    # Root simulator call
    root = CallFrame(
        call_type="CALL",
        from_addr="0x0000000000000000000000000000000000000000",
        to_addr="0x57fa2abf1dc109c5f7ea2fb6a72358d2c624971d",
        input_data="0xb759598a",
        output_data="0x",
        gas=8000000,
        gas_used=300000,
        value=0,
        depth=0,
        calls=[atlas_exec],
    )

    return TraceResult(success=True, root_frame=root)


def _build_0402_trace() -> TraceResult:
    """Reconstruct trace from pdata0402: StaleReport() in oracle call."""
    # StaleReport() selector: 0xf803a2ca
    oracle_revert = CallFrame(
        call_type="STATICCALL",
        from_addr="0xe15bba987c002ecc3586e81244517877d294d291",
        to_addr="0x639fe6ab55c921f74e7fac1ee960c0b6293ba612",
        input_data="0xfeaf968c",  # latestRoundData()
        output_data="0xf803a2ca",
        gas=100000,
        gas_used=5000,
        value=0,
        error="execution reverted",
        depth=4,
    )

    dapp_preops = CallFrame(
        call_type="CALL",
        from_addr="0x8ad1ae9d97c79aa68a0a151e83ff3942f68f86c1",
        to_addr="0xe15bba987c002ecc3586e81244517877d294d291",
        input_data="0x02a688ed",
        output_data="0x",
        gas=500000,
        gas_used=80000,
        value=0,
        error="execution reverted",
        depth=3,
        calls=[oracle_revert],
    )

    atlas_call = CallFrame(
        call_type="CALL",
        from_addr="0x57fa2abf1dc109c5f7ea2fb6a72358d2c624971d",
        to_addr="0x8ad1ae9d97c79aa68a0a151e83ff3942f68f86c1",
        input_data="0xb759598a",
        output_data="0x",
        gas=6000000,
        gas_used=120000,
        value=0,
        error="execution reverted",
        depth=1,
        calls=[dapp_preops],
    )

    root = CallFrame(
        call_type="CALL",
        from_addr="0x0000000000000000000000000000000000000000",
        to_addr="0x57fa2abf1dc109c5f7ea2fb6a72358d2c624971d",
        input_data="0xb759598a",
        output_data="0x",
        gas=8000000,
        gas_used=200000,
        value=0,
        depth=0,
        calls=[atlas_call],
    )

    return TraceResult(success=True, root_frame=root)


def test_0404():
    print("=" * 60)
    print("  TEST: pdata0404 - SolverOpReverted (WETH burn exceeds balance)")
    print("=" * 60)
    print()

    trace = _build_0404_trace()
    assert trace.has_trace

    # Find reverts
    all_reverts = find_all_reverts(trace.root_frame)
    print(f"  Total reverted frames: {len(all_reverts)}")
    assert len(all_reverts) > 0

    deepest = find_deepest_revert(trace.root_frame)
    print(f"  Deepest revert depth: {deepest.depth}")
    print(f"  Deepest revert target: {deepest.to_addr}")
    assert deepest.to_addr == "0x82af49447d8a07e3bd95bd0d56f35241523fbab1"
    assert deepest.depth == 5

    # Print call tree
    print()
    print("  Call Tree:")
    print_call_tree(trace.root_frame, max_depth=8)

    # Analyze
    diagnoses = analyze_trace(
        trace,
        solver_addr="0xf7527ada8c19796d9db0fc629a9895d699470628",
    )

    print()
    print("  Analysis:")
    print(format_diagnoses(diagnoses))

    # Verify root cause detected
    root_causes = [d for d in diagnoses if d.severity == "root_cause"]
    assert len(root_causes) == 1
    assert "WETH" in root_causes[0].title or "Balance" in root_causes[0].title
    print("  PASS: Root cause correctly identified as insufficient WETH balance")
    print()


def test_0402():
    print("=" * 60)
    print("  TEST: pdata0402 - UserOpSimFail (StaleReport)")
    print("=" * 60)
    print()

    trace = _build_0402_trace()
    assert trace.has_trace

    all_reverts = find_all_reverts(trace.root_frame)
    print(f"  Total reverted frames: {len(all_reverts)}")

    deepest = find_deepest_revert(trace.root_frame)
    print(f"  Deepest revert depth: {deepest.depth}")
    print(f"  Deepest revert target: {deepest.to_addr}")
    assert deepest.depth == 4

    print()
    print("  Call Tree:")
    print_call_tree(trace.root_frame, max_depth=8)

    diagnoses = analyze_trace(trace)

    print()
    print("  Analysis:")
    print(format_diagnoses(diagnoses))

    root_causes = [d for d in diagnoses if d.severity == "root_cause"]
    assert len(root_causes) == 1
    assert "StaleReport" in root_causes[0].title
    print("  PASS: Root cause correctly identified as StaleReport")
    print()


if __name__ == "__main__":
    test_0404()
    test_0402()
    print("=" * 60)
    print("  ALL TESTS PASSED")
    print("=" * 60)
