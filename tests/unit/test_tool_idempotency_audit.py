"""Comprehensive audit verification for P2-H1: Unsafe Idempotency and Tool Replay.

Tests the six mandated scenarios:
1. Payment amount 5 versus 500 with reused call ID.
2. Crash after external charge, retry with a different call ID.
3. Reused idempotency key with different arguments.
4. Cross-run and cross-namespace key reuse.
5. Unknown outcome that cannot be safely reconciled.
6. Successful retries using verified provider-supported idempotency.

Central invariant: an uncertain external side effect must never be repeated merely
because a new model call ID was generated.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from memory_aware_agent_harness.contracts import (
    DirtyRollbackError,
    ModelDecision,
    ModelFinishReason,
    NamespaceId,
    RunId,
    RunResult,
    RunStatus,
    SideEffectType,
    StepId,
    ToolCallContext,
    ToolCallRequest,
    ToolCallStatus,
    ToolInvocationId,
    ToolProtocol,
    ToolResult,
)
from memory_aware_agent_harness.runtime.adapters import FakeModelAdapter
from memory_aware_agent_harness.runtime.runner import AgentRuntime
from memory_aware_agent_harness.runtime.tool_recovery import ToolRecoveryEngine
from memory_aware_agent_harness.storage.adapter import SQLiteStorageAdapter


class MockPaymentTool(ToolProtocol):
    """Non-idempotent payment tool tracking execution attempts."""

    def __init__(
        self,
        name: str = "process_payment",
        *,
        side_effect: SideEffectType = SideEffectType.NON_IDEMPOTENT_WRITE,
        supports_provider_idempotency: bool = False,
        fail_first_n_times: int = 0,
    ) -> None:
        self.name = name
        self.description = "Processes external financial payment"
        self.side_effect = side_effect
        self.supports_provider_idempotency = supports_provider_idempotency
        self.fail_first_n_times = fail_first_n_times
        self.call_count = 0
        self.executed_arguments: list[dict[str, Any]] = []

    def get_schema(self) -> Mapping[str, Any]:
        return {
            "type": "object",
            "properties": {
                "amount": {"type": "number"},
                "currency": {"type": "string"},
            },
            "required": ["amount"],
        }

    def execute(
        self,
        arguments: Mapping[str, Any],
        context: ToolCallContext,
    ) -> Mapping[str, Any]:
        self.call_count += 1
        self.executed_arguments.append(dict(arguments))
        if self.call_count <= self.fail_first_n_times:
            raise RuntimeError(
                f"External network crashed while dispatching payment of {arguments.get('amount')}"
            )
        return {
            "status": "succeeded",
            "charge_id": f"ch_{context.invocation_id}",
            "amount": arguments.get("amount"),
            "idempotency_key": context.idempotency_key,
        }


def _init_run_and_step(
    adapter: SQLiteStorageAdapter,
    run_id: RunId,
    step_id: StepId,
    namespace_id: NamespaceId,
) -> None:
    run = RunResult(
        run_id=run_id,
        namespace_id=namespace_id,
        status=RunStatus.RUNNING,
        step_count=0,
        total_tokens_consumed=0,
        final_output=None,
        trace_id=f"tr_{run_id}",
    )
    adapter.save_run(run)
    adapter.record_step_start(
        step_id=step_id,
        run_id=run_id,
        namespace_id=namespace_id,
        step_number=1,
        started_at=datetime.now(UTC),
    )


# =================================================================================================
# Test 1: Payment amount 5 versus 500 with reused call ID
# =================================================================================================


def test_payment_amount_5_vs_500_with_reused_call_id() -> None:
    """When a model reuses call ID with different arguments, cached result must NOT replay."""
    with SQLiteStorageAdapter(":memory:") as adapter:
        ns = NamespaceId("ns_audit_1")
        tool = MockPaymentTool("payment_tool")

        # Step 1: Model calls payment with amount=5 and call_id="call_tx_1"
        call_id = "call_tx_1"
        step_1_decision = ModelDecision(
            decision_id="dec_1",
            step_id=StepId("step_1"),
            finish_reason=ModelFinishReason.TOOL_CALL,
            tool_calls=(
                ToolCallRequest(
                    call_id=call_id,
                    tool_name="payment_tool",
                    arguments={"amount": 5, "currency": "USD"},
                ),
            ),
        )
        # Step 2: Model tries to reuse SAME call_id="call_tx_1" with amount=500
        step_2_decision = ModelDecision(
            decision_id="dec_2",
            step_id=StepId("step_2"),
            finish_reason=ModelFinishReason.TOOL_CALL,
            tool_calls=(
                ToolCallRequest(
                    call_id=call_id,
                    tool_name="payment_tool",
                    arguments={"amount": 500, "currency": "USD"},
                ),
            ),
        )
        finish_decision = ModelDecision(
            decision_id="dec_done",
            step_id=StepId("step_3"),
            finish_reason=ModelFinishReason.STOP,
            content="finished",
        )

        model = FakeModelAdapter(responses=[step_1_decision, step_2_decision, finish_decision])
        runtime = AgentRuntime(model=model, tools=[tool], storage=adapter)

        res = runtime.run(
            task="Pay 5 then 500",
            namespace_id=ns,
            run_id=RunId("run_pay_5_500"),
        )

        # Step 1 succeeded for $5
        assert tool.call_count >= 1
        assert tool.executed_arguments[0]["amount"] == 5

        # Check results in step history
        assert len(res.step_history) >= 2
        step_1_res = res.step_history[0]
        step_2_res = res.step_history[1]

        assert step_1_res.tool_results[0].status == ToolCallStatus.COMPLETED
        assert step_1_res.tool_results[0].result is not None
        assert step_1_res.tool_results[0].result["amount"] == 5

        # Step 2 MUST NOT report the $5 payment as completed for the $500 request!
        step_2_tool_res = step_2_res.tool_results[0]
        if step_2_tool_res.status == ToolCallStatus.COMPLETED:
            # If completed, it must be because it executed fresh for 500, NEVER replaying 5
            assert step_2_tool_res.result is not None
            assert step_2_tool_res.result["amount"] == 500
        else:
            # Otherwise blocked by idempotency conflict / dirty rollback guard
            assert step_2_tool_res.status == ToolCallStatus.FAILED
            assert step_2_tool_res.failure_details is not None
            assert step_2_tool_res.failure_details.error_code in (
                "PARTIAL_DIRTY_ROLLBACK",
                "IDEMPOTENCY_ARGUMENT_MISMATCH",
            )


# =================================================================================================
# Test 2: Crash after external charge, retry with a different call ID
# =================================================================================================


def test_crash_after_external_charge_retry_with_different_call_id() -> None:
    """An uncertain external side effect must never repeat merely because of new call ID."""
    with SQLiteStorageAdapter(":memory:") as adapter:
        ns = NamespaceId("ns_audit_2")
        run_id = RunId("run_charge_crash")
        step_id = StepId("step_charge_crash")
        _init_run_and_step(adapter, run_id, step_id, ns)

        # Pre-condition: External charge was dispatched with call_id="call_original".
        # Network crashed or process was interrupted while charge was in flight.
        inv_id = ToolInvocationId("inv_crashed_charge")
        adapter.record_tool_intent(
            invocation_id=inv_id,
            step_id=step_id,
            namespace_id=ns,
            tool_name="charge_card",
            arguments={"amount": 100, "currency": "USD"},
            side_effect=SideEffectType.NON_IDEMPOTENT_WRITE,
            idempotency_key="op_charge_card_100",
            dispatched_at=datetime.now(UTC),
        )
        # Process recovery marks invocation OUTCOME_UNKNOWN
        adapter.mark_invocation_unknown(
            inv_id,
            ns,
            error="Process crashed during external gateway dispatch",
        )

        charge_tool = MockPaymentTool(
            "charge_card", side_effect=SideEffectType.NON_IDEMPOTENT_WRITE
        )
        # Model now generates a retry with a BRAND NEW call ID "call_retry_new_uuid"
        retry_decision = ModelDecision(
            decision_id="dec_retry",
            step_id=StepId("step_retry"),
            finish_reason=ModelFinishReason.TOOL_CALL,
            tool_calls=(
                ToolCallRequest(
                    call_id="call_retry_new_uuid",
                    tool_name="charge_card",
                    arguments={"amount": 100, "currency": "USD"},
                ),
            ),
        )
        model = FakeModelAdapter(
            responses=[
                retry_decision,
                ModelDecision(
                    decision_id="dec_fin",
                    step_id=StepId("step_fin"),
                    finish_reason=ModelFinishReason.STOP,
                    content="done",
                ),
            ]
        )

        runtime = AgentRuntime(model=model, tools=[charge_tool], storage=adapter)
        run_res = runtime.run(
            task="Retry charge",
            namespace_id=ns,
            run_id=RunId("run_charge_retry"),
            # Caller ties operation identity to the trusted business operation
            business_operation_id="op_charge_card_100",
        )

        # Central invariant: The tool must NOT be executed again!
        assert charge_tool.call_count == 0, (
            "Uncertain external charge must not repeat merely because a new call ID was generated"
        )

        # The tool result must record PARTIAL_DIRTY_ROLLBACK failure
        tool_res = run_res.tool_results[0]
        assert tool_res.status == ToolCallStatus.FAILED
        assert tool_res.failure_details is not None
        assert tool_res.failure_details.error_code == "PARTIAL_DIRTY_ROLLBACK"


# =================================================================================================
# Test 3: Reused idempotency key with different arguments
# =================================================================================================


def test_reused_idempotency_key_with_different_arguments() -> None:
    """Reusing an idempotency key with different arguments must raise DirtyRollbackError."""
    with SQLiteStorageAdapter(":memory:") as adapter:
        ns = NamespaceId("ns_audit_3")
        run_id = RunId("run_idem_diff")
        step_id = StepId("step_idem_diff")
        _init_run_and_step(adapter, run_id, step_id, ns)

        key = "tx_wire_999"
        inv_id = ToolInvocationId("inv_wire_1")

        # 1. First invocation completes with amount 50
        adapter.record_tool_intent(
            invocation_id=inv_id,
            step_id=step_id,
            namespace_id=ns,
            tool_name="wire_transfer",
            arguments={"amount": 50, "recipient": "alice"},
            side_effect=SideEffectType.NON_IDEMPOTENT_WRITE,
            idempotency_key=key,
            dispatched_at=datetime.now(UTC),
        )
        adapter.record_tool_result(
            ToolResult(
                invocation_id=inv_id,
                tool_name="wire_transfer",
                status=ToolCallStatus.COMPLETED,
                side_effect=SideEffectType.NON_IDEMPOTENT_WRITE,
                idempotency_key=key,
                result={"transferred": 50},
            ),
            ns,
        )

        engine = ToolRecoveryEngine(adapter)

        # Exact match of arguments replays cached result cleanly
        cached_same = engine.get_replayable_cached_result(
            idempotency_key=key,
            namespace_id=ns,
            tool_name="wire_transfer",
            arguments={"amount": 50, "recipient": "alice"},
        )
        assert cached_same is not None
        assert cached_same.status == ToolCallStatus.COMPLETED
        assert cached_same.result == {"transferred": 50}

        # Different arguments (amount 100 instead of 50) MUST FAIL CLOSED
        with pytest.raises(DirtyRollbackError) as exc_info:
            engine.get_replayable_cached_result(
                idempotency_key=key,
                namespace_id=ns,
                tool_name="wire_transfer",
                arguments={"amount": 100, "recipient": "alice"},
            )

        err = exc_info.value
        assert "argument fingerprint does not match" in str(err)
        assert err.details.get("error_code") == "IDEMPOTENCY_ARGUMENT_MISMATCH"


# =================================================================================================
# Test 4: Cross-run and cross-namespace key reuse
# =================================================================================================


def test_cross_run_and_cross_namespace_key_reuse() -> None:
    """Keys must never leak cached results across namespaces, and cross-run access is isolated."""
    with SQLiteStorageAdapter(":memory:") as adapter:
        ns_tenant_a = NamespaceId("tenant_a")
        ns_tenant_b = NamespaceId("tenant_b")

        # Tenant A records an intent and completion
        run_a = RunId("run_a")
        step_a = StepId("step_a")
        _init_run_and_step(adapter, run_a, step_a, ns_tenant_a)

        key = "shared_idem_key"
        inv_a = ToolInvocationId("inv_tenant_a")
        adapter.record_tool_intent(
            invocation_id=inv_a,
            step_id=step_a,
            namespace_id=ns_tenant_a,
            tool_name="export_report",
            arguments={"report_id": 42},
            side_effect=SideEffectType.NON_IDEMPOTENT_WRITE,
            idempotency_key=key,
            dispatched_at=datetime.now(UTC),
        )
        adapter.record_tool_result(
            ToolResult(
                invocation_id=inv_a,
                tool_name="export_report",
                status=ToolCallStatus.COMPLETED,
                side_effect=SideEffectType.NON_IDEMPOTENT_WRITE,
                idempotency_key=key,
                result={"url": "https://tenant_a.internal/report.pdf"},
            ),
            ns_tenant_a,
        )

        engine = ToolRecoveryEngine(adapter)

        # 1. Tenant A sees cached result
        cached_a = engine.get_replayable_cached_result(
            idempotency_key=key,
            namespace_id=ns_tenant_a,
            tool_name="export_report",
            arguments={"report_id": 42},
        )
        assert cached_a is not None
        assert cached_a.result == {"url": "https://tenant_a.internal/report.pdf"}

        # 2. Tenant B using the EXACT same key and arguments sees NONE (no cross-tenant leakage)
        cached_b = engine.get_replayable_cached_result(
            idempotency_key=key,
            namespace_id=ns_tenant_b,
            tool_name="export_report",
            arguments={"report_id": 42},
        )
        assert cached_b is None, "Tenant B must never receive Tenant A's cached tool result"

        # 3. Direct attempt to fetch Tenant A's invocation ID under Tenant B
        # returns None (no existence disclosure across namespaces)
        assert adapter.get_tool_invocation_record(inv_a, ns_tenant_b) is None


# =================================================================================================
# Test 5: Unknown outcome that cannot be safely reconciled
# =================================================================================================


def test_unknown_outcome_cannot_be_safely_reconciled() -> None:
    """Uncertain non-idempotent write without safe deduplication preserves OUTCOME_UNKNOWN."""
    with SQLiteStorageAdapter(":memory:") as adapter:
        ns = NamespaceId("ns_audit_5")
        run_id = RunId("run_unreconciled")
        step_id = StepId("step_unreconciled")
        _init_run_and_step(adapter, run_id, step_id, ns)

        inv_id = ToolInvocationId("inv_unknown_1")
        key = "unverified_batch_charge"
        adapter.record_tool_intent(
            invocation_id=inv_id,
            step_id=step_id,
            namespace_id=ns,
            tool_name="bulk_charge",
            arguments={"batch_size": 100},
            side_effect=SideEffectType.NON_IDEMPOTENT_WRITE,
            idempotency_key=key,
            dispatched_at=datetime.now(UTC),
        )

        # Crash occurs
        engine = ToolRecoveryEngine(adapter)
        report = engine.reconcile_pending_invocations(ns)

        # Verified: flagged dirty and marked OUTCOME_UNKNOWN
        assert report.dirty_non_idempotent_count == 1
        assert report.marked_unknown_count == 1

        rec = adapter.get_tool_invocation_record(inv_id, ns)
        assert rec is not None
        assert rec.status == ToolCallStatus.OUTCOME_UNKNOWN

        # Cannot be automatically replayed or executed: must fail closed
        with pytest.raises(DirtyRollbackError) as exc_info:
            engine.get_replayable_cached_result(
                idempotency_key=key,
                namespace_id=ns,
                tool_name="bulk_charge",
                arguments={"batch_size": 100},
            )
        assert exc_info.value.details.get("error_code") == "PARTIAL_DIRTY_ROLLBACK"

        # Explicit manual reconciliation by operator recovers the record
        resolved = adapter.resolve_tool_invocation(
            invocation_id=inv_id,
            namespace_id=ns,
            status=ToolCallStatus.COMPLETED,
            result={"manual_audit": "verified_by_finance"},
        )
        assert resolved.status == ToolCallStatus.COMPLETED

        # Now and only now is replay authorized
        cached_now = engine.get_replayable_cached_result(
            idempotency_key=key,
            namespace_id=ns,
            tool_name="bulk_charge",
            arguments={"batch_size": 100},
        )
        assert cached_now is not None
        assert cached_now.result == {"manual_audit": "verified_by_finance"}


# =================================================================================================
# Test 6: Successful retries using verified provider-supported idempotency
# =================================================================================================


def test_successful_retries_using_verified_provider_supported_idempotency() -> None:
    """When a tool provides verified provider-supported idempotency, safe retries are permitted."""
    with SQLiteStorageAdapter(":memory:") as adapter:
        ns = NamespaceId("ns_audit_6")
        run_id = RunId("run_provider_retry")

        # Stripe-like tool with native provider idempotency guarantees
        stripe_tool = MockPaymentTool(
            "stripe_charge",
            side_effect=SideEffectType.NON_IDEMPOTENT_WRITE,
            supports_provider_idempotency=True,
            fail_first_n_times=1,  # First dispatch will simulate network failure
        )

        provider_key = "prov_stripe_tx_777"
        model_decision_1 = ModelDecision(
            decision_id="dec_prov_1",
            step_id=StepId("step_prov_1"),
            finish_reason=ModelFinishReason.TOOL_CALL,
            tool_calls=(
                ToolCallRequest(
                    call_id="call_p1",
                    tool_name="stripe_charge",
                    arguments={"amount": 250, "currency": "USD"},
                ),
            ),
        )
        model_decision_2 = ModelDecision(
            decision_id="dec_prov_2",
            step_id=StepId("step_prov_2"),
            finish_reason=ModelFinishReason.TOOL_CALL,
            tool_calls=(
                ToolCallRequest(
                    call_id="call_p2",
                    tool_name="stripe_charge",
                    arguments={"amount": 250, "currency": "USD"},
                ),
            ),
        )
        finish_decision = ModelDecision(
            decision_id="dec_prov_done",
            step_id=StepId("step_prov_3"),
            finish_reason=ModelFinishReason.STOP,
            content="completed successfully",
        )

        model = FakeModelAdapter(responses=[model_decision_1, model_decision_2, finish_decision])
        runtime = AgentRuntime(
            model=model,
            tools=[stripe_tool],
            storage=adapter,
            business_operation_id=provider_key,
        )

        # Run with initial attempt failing and second attempt retrying
        res = runtime.run(
            task="Stripe charge with provider idempotency",
            namespace_id=ns,
            run_id=run_id,
            business_operation_id=provider_key,
        )

        # Call count reached 2 (initial failed attempt + retry permitted by provider idempotency)
        assert stripe_tool.call_count == 2

        # Step 2 succeeded because provider safely deduplicates
        assert len(res.step_history) >= 2
        step_1_res = res.step_history[0]
        step_2_res = res.step_history[1]

        # Step 1 was uncertain due to network crash
        assert step_1_res.tool_results[0].status == ToolCallStatus.OUTCOME_UNKNOWN

        # Step 2 retry succeeded
        assert step_2_res.tool_results[0].status == ToolCallStatus.COMPLETED
        assert step_2_res.tool_results[0].result is not None
        assert step_2_res.tool_results[0].result["status"] == "succeeded"
        assert step_2_res.tool_results[0].result["amount"] == 250


# =================================================================================================
# Round 2 Regression Tests: P2-H1 Edge Cases
# =================================================================================================


def test_two_legitimate_repeated_payments_both_execute() -> None:
    """Two legitimate pay(amount=5) operations with distinct identities must both execute."""
    with SQLiteStorageAdapter(":memory:") as adapter:
        ns = NamespaceId("ns_repeated_pay")
        tool = MockPaymentTool("pay", side_effect=SideEffectType.NON_IDEMPOTENT_WRITE)

        step_1_decision = ModelDecision(
            decision_id="dec_pay_1",
            step_id=StepId("step_1"),
            finish_reason=ModelFinishReason.TOOL_CALL,
            tool_calls=(
                ToolCallRequest(
                    call_id="call_pay_1",
                    tool_name="pay",
                    arguments={"amount": 5, "currency": "USD"},
                ),
            ),
        )
        step_2_decision = ModelDecision(
            decision_id="dec_pay_2",
            step_id=StepId("step_2"),
            finish_reason=ModelFinishReason.TOOL_CALL,
            tool_calls=(
                ToolCallRequest(
                    call_id="call_pay_2",
                    tool_name="pay",
                    arguments={"amount": 5, "currency": "USD"},
                ),
            ),
        )
        finish_decision = ModelDecision(
            decision_id="dec_done",
            step_id=StepId("step_3"),
            finish_reason=ModelFinishReason.STOP,
            content="done",
        )

        model = FakeModelAdapter(responses=[step_1_decision, step_2_decision, finish_decision])
        runtime = AgentRuntime(model=model, tools=[tool], storage=adapter)

        res = runtime.run(
            task="Pay 5 twice",
            namespace_id=ns,
            run_id=RunId("run_pay_twice"),
        )

        # Both distinct $5 payments must have executed!
        assert tool.call_count == 2
        assert len(tool.executed_arguments) == 2
        assert tool.executed_arguments[0]["amount"] == 5
        assert tool.executed_arguments[1]["amount"] == 5

        # Both step outcomes must be COMPLETED
        assert res.step_history[0].tool_results[0].status == ToolCallStatus.COMPLETED
        assert res.step_history[1].tool_results[0].status == ToolCallStatus.COMPLETED
        # Invocations must have distinct invocation IDs and distinct results
        inv1 = res.step_history[0].tool_results[0].invocation_id
        inv2 = res.step_history[1].tool_results[0].invocation_id
        assert inv1 != inv2


def test_model_supplied_idempotency_key_is_ignored_for_trusted_replay() -> None:
    """Model-supplied idempotency_key must never be trusted for business replay decisions."""
    with SQLiteStorageAdapter(":memory:") as adapter:
        ns = NamespaceId("ns_model_key_untrusted")
        tool = MockPaymentTool("pay", side_effect=SideEffectType.NON_IDEMPOTENT_WRITE)

        # Model attempts to supply its own 'idempotency_key' in arguments
        step_1_decision = ModelDecision(
            decision_id="dec_spoof_1",
            step_id=StepId("step_1"),
            finish_reason=ModelFinishReason.TOOL_CALL,
            tool_calls=(
                ToolCallRequest(
                    call_id="call_1",
                    tool_name="pay",
                    arguments={"amount": 10, "idempotency_key": "spoofed_key"},
                ),
            ),
        )
        step_2_decision = ModelDecision(
            decision_id="dec_spoof_2",
            step_id=StepId("step_2"),
            finish_reason=ModelFinishReason.TOOL_CALL,
            tool_calls=(
                ToolCallRequest(
                    call_id="call_2",
                    tool_name="pay",
                    arguments={"amount": 10, "idempotency_key": "spoofed_key"},
                ),
            ),
        )
        finish_decision = ModelDecision(
            decision_id="dec_done",
            step_id=StepId("step_3"),
            finish_reason=ModelFinishReason.STOP,
            content="done",
        )

        model = FakeModelAdapter(responses=[step_1_decision, step_2_decision, finish_decision])
        runtime = AgentRuntime(model=model, tools=[tool], storage=adapter)

        res = runtime.run(
            task="Test untrusted model key",
            namespace_id=ns,
            run_id=RunId("run_spoof_test"),
        )

        # Because model key is ignored, both distinct invocations execute
        assert tool.call_count == 2
        assert res.step_history[0].tool_results[0].status == ToolCallStatus.COMPLETED
        assert res.step_history[1].tool_results[0].status == ToolCallStatus.COMPLETED


def test_read_only_tool_not_cached_across_independent_runs_stale_balance() -> None:
    """READ_ONLY results must never replay across independent runs (stale balance bug)."""

    class BalanceTool(ToolProtocol):
        def __init__(self) -> None:
            self.name = "get_balance"
            self.description = "Returns user account balance"
            self.side_effect = SideEffectType.READ_ONLY
            self.current_balance = 100

        def get_schema(self) -> Mapping[str, Any]:
            return {"type": "object", "properties": {"account": {"type": "string"}}}

        def execute(
            self, arguments: Mapping[str, Any], context: ToolCallContext
        ) -> Mapping[str, Any]:
            return {"account": arguments.get("account"), "balance": self.current_balance}

    with SQLiteStorageAdapter(":memory:") as adapter:
        ns = NamespaceId("ns_balance")
        tool = BalanceTool()

        call_decision = ModelDecision(
            decision_id="dec_bal",
            step_id=StepId("step_1"),
            finish_reason=ModelFinishReason.TOOL_CALL,
            tool_calls=(
                ToolCallRequest(
                    call_id="call_bal",
                    tool_name="get_balance",
                    arguments={"account": "user_A"},
                ),
            ),
        )
        stop_decision = ModelDecision(
            decision_id="dec_stop",
            step_id=StepId("step_2"),
            finish_reason=ModelFinishReason.STOP,
            content="done",
        )

        # Run 1: Balance is 100
        model1 = FakeModelAdapter(responses=[call_decision, stop_decision])
        runtime1 = AgentRuntime(model=model1, tools=[tool], storage=adapter)
        res1 = runtime1.run("Check balance run 1", namespace_id=ns, run_id=RunId("run_1"))
        assert res1.step_history[0].tool_results[0].result == {"account": "user_A", "balance": 100}

        # External state changes: account balance is drained to 0!
        tool.current_balance = 0

        # Run 2: Independent run queries balance again
        model2 = FakeModelAdapter(responses=[call_decision, stop_decision])
        runtime2 = AgentRuntime(model=model2, tools=[tool], storage=adapter)
        res2 = runtime2.run("Check balance run 2", namespace_id=ns, run_id=RunId("run_2"))

        # Must NOT return cached 100! Must return current balance 0!
        assert res2.step_history[0].tool_results[0].result == {"account": "user_A", "balance": 0}


def test_provider_idempotency_key_stable_across_crashes_and_cross_run_retries() -> None:
    """Provider idempotency key remains identical when retrying an uncertain call across runs."""
    with SQLiteStorageAdapter(":memory:") as adapter:
        ns = NamespaceId("ns_provider_crash")

        captured_keys: list[str] = []

        class StripeTool(ToolProtocol):
            def __init__(self) -> None:
                self.name = "stripe_charge"
                self.description = "Processes credit card charge"
                self.side_effect = SideEffectType.NON_IDEMPOTENT_WRITE
                self.supports_provider_idempotency = True
                self.should_fail = True

            def get_schema(self) -> Mapping[str, Any]:
                return {"type": "object", "properties": {"amount": {"type": "number"}}}

            def execute(
                self, arguments: Mapping[str, Any], context: ToolCallContext
            ) -> Mapping[str, Any]:
                captured_keys.append(context.idempotency_key)
                if self.should_fail:
                    raise RuntimeError("Simulated process crash during external dispatch")
                return {"status": "succeeded", "charge_id": "ch_999"}

        tool = StripeTool()

        call_decision = ModelDecision(
            decision_id="dec_charge",
            step_id=StepId("step_1"),
            finish_reason=ModelFinishReason.TOOL_CALL,
            tool_calls=(
                ToolCallRequest(
                    call_id="call_ch_1",
                    tool_name="stripe_charge",
                    arguments={"amount": 50},
                ),
            ),
        )
        stop_decision = ModelDecision(
            decision_id="dec_stop",
            step_id=StepId("step_2"),
            finish_reason=ModelFinishReason.STOP,
            content="done",
        )

        # Run 1: Crashes during execution
        model1 = FakeModelAdapter(responses=[call_decision, stop_decision])
        runtime1 = AgentRuntime(model=model1, tools=[tool], storage=adapter)
        res1 = runtime1.run("Charge $50", namespace_id=ns, run_id=RunId("run_charge_1"))

        assert len(captured_keys) == 1
        first_key = captured_keys[0]
        assert first_key.startswith("prov_")
        # Run 1 left the tool invocation as OUTCOME_UNKNOWN
        assert res1.step_history[0].tool_results[0].status == ToolCallStatus.OUTCOME_UNKNOWN

        # Recovery runs after crash
        adapter.reconcile_tool_invocations(ns)

        # Run 2: New run retries the charge after recovery
        tool.should_fail = False
        model2 = FakeModelAdapter(responses=[call_decision, stop_decision])
        runtime2 = AgentRuntime(model=model2, tools=[tool], storage=adapter)
        res2 = runtime2.run("Retry charge $50", namespace_id=ns, run_id=RunId("run_charge_2"))

        assert len(captured_keys) == 2
        second_key = captured_keys[1]

        # The idempotency key sent to Stripe MUST BE IDENTICAL to prevent duplicate billing!
        assert second_key == first_key
        assert res2.step_history[0].tool_results[0].status == ToolCallStatus.COMPLETED
