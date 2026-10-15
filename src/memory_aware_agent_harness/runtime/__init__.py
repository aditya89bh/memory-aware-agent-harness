"""Core single-agent runtime for memory-aware execution.

Provides finite state machines, lifecycle management, budget enforcement,
cancellation listeners, and deterministic execution controllers.
"""

from __future__ import annotations

from memory_aware_agent_harness.runtime.adapters import FakeModelAdapter
from memory_aware_agent_harness.runtime.cancellation import (
    AbortDispatcher,
    CancellationListener,
)
from memory_aware_agent_harness.runtime.events import (
    DiagnosticEventEmitter,
    DiagnosticHook,
    DiagnosticListenerError,
    EventCollector,
    RuntimeEvent,
    RuntimeEventListener,
    RuntimeEventType,
)
from memory_aware_agent_harness.runtime.fsm import RunFSM, StepFSM
from memory_aware_agent_harness.runtime.guards import BudgetGuard
from memory_aware_agent_harness.runtime.recovery import (
    OrphanedRunError,
    OrphanedRunRecoveryHandler,
)
from memory_aware_agent_harness.runtime.rehydration import (
    CheckpointRehydrationEngine,
    RehydratedState,
    rehydrate_checkpoint_state,
)
from memory_aware_agent_harness.runtime.result_builder import RunResultBuilder
from memory_aware_agent_harness.runtime.runner import AgentRuntime
from memory_aware_agent_harness.runtime.state import (
    LifecycleState,
    SystemClock,
    SystemIdGenerator,
)
from memory_aware_agent_harness.runtime.step_compiler import StepOutcomeCompiler

__all__ = [
    "AbortDispatcher",
    "AgentRuntime",
    "BudgetGuard",
    "CancellationListener",
    "CheckpointRehydrationEngine",
    "DiagnosticEventEmitter",
    "DiagnosticHook",
    "DiagnosticListenerError",
    "EventCollector",
    "FakeModelAdapter",
    "LifecycleState",
    "OrphanedRunError",
    "OrphanedRunRecoveryHandler",
    "RehydratedState",
    "RunFSM",
    "RunResultBuilder",
    "RuntimeEvent",
    "RuntimeEventListener",
    "RuntimeEventType",
    "StepFSM",
    "StepOutcomeCompiler",
    "SystemClock",
    "SystemIdGenerator",
    "rehydrate_checkpoint_state",
]
