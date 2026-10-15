"""Unit tests for pre-persistence security, provenance validation, and credential redaction."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from memory_aware_agent_harness.contracts import (
    MemoryId,
    MemoryKind,
    MemoryOrigin,
    MemoryRecord,
    MemoryState,
    NamespaceId,
    PrePersistenceSecurityError,
    RunId,
    RunResult,
    RunStatus,
    StepId,
    StepResult,
)
from memory_aware_agent_harness.storage.adapter import SQLiteStorageAdapter
from memory_aware_agent_harness.storage.security import (
    PrePersistenceValidator,
    redact_secrets,
)
from memory_aware_agent_harness.storage.step_commit import StepCommitBundle


def _make_mem(
    mem_id: str,
    ns: str,
    content: str,
    origin: MemoryOrigin = MemoryOrigin.AGENT_OBSERVATION,
    metadata: dict[str, str] | None = None,
) -> MemoryRecord:
    now = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)
    return MemoryRecord(
        id=MemoryId(mem_id),
        namespace_id=NamespaceId(ns),
        content=content,
        kind=MemoryKind.SEMANTIC,
        state=MemoryState.ACTIVE,
        confidence=0.9,
        salience=0.8,
        origin=origin,
        metadata=metadata or {},
        created_at=now,
        updated_at=now,
    )


def test_provenance_validation_lowest_trust_fallback() -> None:
    """Omitted origin must fall back to lowest-trust EXTERNAL_RETRIEVAL, never SYSTEM_PROVIDED."""
    validator = PrePersistenceValidator()
    resolved = validator.validate_provenance(None, namespace_id="ns1", record_id="rec1")
    assert resolved == MemoryOrigin.EXTERNAL_RETRIEVAL
    assert resolved.value == "external_retrieval"


def test_provenance_validation_rejects_malformed_and_unauthorized() -> None:
    """Malformed origin strings or unauthorized SYSTEM_PROVIDED claims are rejected."""
    validator = PrePersistenceValidator()

    # Malformed / invalid origin
    with pytest.raises(PrePersistenceSecurityError) as exc_info:
        validator.validate_provenance("forged_root_admin", namespace_id="ns1", record_id="r1")
    assert exc_info.value.rule_name == "MALFORMED_PROVENANCE_ORIGIN"
    assert exc_info.value.namespace_id == "ns1"

    # Non-string, non-enum object
    with pytest.raises(PrePersistenceSecurityError) as exc_info:
        validator.validate_provenance(12345, namespace_id="ns1", record_id="r1")
    assert exc_info.value.rule_name == "MALFORMED_PROVENANCE_ORIGIN"

    # Unauthorized SYSTEM_PROVIDED claim by external actor
    with pytest.raises(PrePersistenceSecurityError) as exc_info:
        validator.validate_provenance(
            MemoryOrigin.SYSTEM_PROVIDED,
            namespace_id="ns1",
            record_id="r1",
            is_system_actor=False,
        )
    assert exc_info.value.rule_name == "UNAUTHORIZED_SYSTEM_PROVENANCE"

    # Authorized system actor can set SYSTEM_PROVIDED
    assert (
        validator.validate_provenance(
            MemoryOrigin.SYSTEM_PROVIDED,
            namespace_id="ns1",
            record_id="r1",
            is_system_actor=True,
        )
        == MemoryOrigin.SYSTEM_PROVIDED
    )


def test_redact_secrets_provider_api_keys() -> None:
    """Synthetic OpenAI, Anthropic, and GitHub keys are reliably detected and redacted."""
    raw_text = (
        "Found key sk-live9876543210abcdef1234567890 in config, "
        "and token ghp_abcdefghijklmnopqrstuvwxyz0123456789 for repo access."
    )
    result = redact_secrets(raw_text)
    assert result.secrets_detected == 2
    assert "[REDACTED_API_KEY]" in result.cleaned_text
    assert "[REDACTED_GITHUB_TOKEN]" in result.cleaned_text
    assert "sk-live9876543210abcdef1234567890" not in result.cleaned_text
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in result.cleaned_text


def test_redact_secrets_aws_and_bearer_and_private_keys() -> None:
    """AWS keys, bearer tokens, and PEM private keys are reliably scrubbed."""
    raw_text = (
        "AWS credentials: AKIAIOSFODNN7EXAMPLE and "
        "aws_secret_access_key='wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'. "
        "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secretpayload.sig. "
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA0syntheticKeyBody\n"
        "-----END RSA PRIVATE KEY-----"
    )
    result = redact_secrets(raw_text)
    assert result.secrets_detected == 4
    assert "[REDACTED_AWS_KEY]" in result.cleaned_text
    assert "[REDACTED_AWS_SECRET]" in result.cleaned_text
    assert "[REDACTED_BEARER_TOKEN]" in result.cleaned_text
    assert "[REDACTED_PRIVATE_KEY]" in result.cleaned_text
    assert "AKIAIOSFODNN7EXAMPLE" not in result.cleaned_text
    assert "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" not in result.cleaned_text


def test_fail_closed_mode_raises_without_logging_secret() -> None:
    """Fail-closed mode raises PrePersistenceSecurityError and never leaks secret in error."""
    secret = "sk-live1234567890abcdef1234567890"
    validator = PrePersistenceValidator(fail_closed=True)

    with pytest.raises(PrePersistenceSecurityError) as exc_info:
        validator.sanitize_text(
            f"Sensitive prompt text containing {secret}",
            namespace_id="ns_sec",
            record_id="rec_secret",
        )

    err = exc_info.value
    assert err.rule_name == "CREDENTIAL_REDACTION_VIOLATION"
    assert err.namespace_id == "ns_sec"
    assert err.record_id == "rec_secret"

    # CRITICAL: Verify the raw secret is NEVER contained in the exception message or str(err)
    assert secret not in str(err)
    assert secret not in repr(err)
    assert secret not in str(err.details)


def test_sanitize_memory_record_and_nested_metadata() -> None:
    """Nested metadata and record content are sanitized cleanly in non-fail-closed mode."""
    validator = PrePersistenceValidator(fail_closed=False)
    rec = _make_mem(
        "m_sec",
        "ns_sec",
        content="Deploy using sk-test999988887777666655554444",
        metadata={
            "token": "ghp_111122223333444455556666777788889999",
            "nested_text": "Authorization: Bearer mySecretTokenValue1234567890abc",
        },
    )

    clean_rec = validator.sanitize_memory_record(rec)
    assert "[REDACTED_API_KEY]" in clean_rec.content
    assert "sk-test999988887777666655554444" not in clean_rec.content
    assert "[REDACTED_GITHUB_TOKEN]" in clean_rec.metadata["token"]
    assert "[REDACTED_BEARER_TOKEN]" in clean_rec.metadata["nested_text"]


def test_adapter_persistence_with_redaction_boundary() -> None:
    """SQLiteStorageAdapter sanitizes content before persistence into SQLite."""
    ns = NamespaceId("ns_adapter_sec")
    adapter = SQLiteStorageAdapter(":memory:")

    secret_key = "sk-proj1234567890abcdefghijklmnop"
    rec = _make_mem("m_db_sec", str(ns), f"Configuration with secret {secret_key}")

    # Put should sanitize
    adapter.put(rec)

    persisted = adapter.get(MemoryId("m_db_sec"), ns)
    assert persisted is not None
    assert secret_key not in persisted.content
    assert "[REDACTED_API_KEY]" in persisted.content

    # Raw database row also does NOT contain the secret
    row = adapter.conn.execute(
        "SELECT content FROM memory_records WHERE memory_id = 'm_db_sec';"
    ).fetchone()
    assert row is not None
    assert secret_key not in row["content"]

    adapter.close()


def test_adapter_fail_closed_mode_prevents_storage() -> None:
    """When configured with fail-closed validator, storage put rejects and persists nothing."""
    ns = NamespaceId("ns_fail_closed")
    fail_closed_validator = PrePersistenceValidator(fail_closed=True)
    adapter = SQLiteStorageAdapter(":memory:", validator=fail_closed_validator)

    secret = "sk-live1234567890abcdef1234567890"
    rec = _make_mem("m_rejected", str(ns), f"Must fail with secret {secret}")

    with pytest.raises(PrePersistenceSecurityError):
        adapter.put(rec)

    # Verify nothing was persisted
    assert adapter.get(MemoryId("m_rejected"), ns) is None
    row = adapter.conn.execute(
        "SELECT count(*) as cnt FROM memory_records WHERE memory_id = 'm_rejected';"
    ).fetchone()
    assert row is not None and row["cnt"] == 0

    adapter.close()


def test_step_commit_sanitizes_proposed_memories() -> None:
    """4-phase step commit automatically sanitizes proposed memories before storage."""
    ns = NamespaceId("ns_step_sec")
    run_id = RunId("run_step_sec")
    step_id = StepId("step_sec")
    adapter = SQLiteStorageAdapter(":memory:")

    from memory_aware_agent_harness.contracts import ModelDecision, ModelFinishReason

    adapter.save_run(
        RunResult(
            run_id=run_id,
            namespace_id=ns,
            status=RunStatus.RUNNING,
            step_count=0,
            total_tokens_consumed=0,
            final_output=None,
            trace_id="tr_s",
        )
    )

    secret = "sk-live9999888877776666555544443333"
    mem_with_secret = _make_mem("m_step_sec", str(ns), f"Extracted memory with {secret}")

    step_res = StepResult(
        step_id=step_id,
        run_id=run_id,
        step_number=1,
        decision=ModelDecision(
            decision_id="d1",
            step_id=step_id,
            finish_reason=ModelFinishReason.STOP,
            content="done",
        ),
        tool_results=(),
        memories_proposed=1,
        memories_committed=1,
        tokens_consumed=50,
        completed_at=datetime.now(UTC),
    )

    bundle = StepCommitBundle(
        step_result=step_res,
        run_result=RunResult(
            run_id=run_id,
            namespace_id=ns,
            status=RunStatus.RUNNING,
            step_count=1,
            total_tokens_consumed=50,
            final_output=None,
            trace_id="tr_s",
        ),
        namespace_id=ns,
        proposed_memories=[mem_with_secret],
    )

    outcome = adapter.commit_step(bundle)
    assert outcome.memories_committed == 1

    stored_mem = adapter.get(MemoryId("m_step_sec"), ns)
    assert stored_mem is not None
    assert secret not in stored_mem.content
    assert "[REDACTED_API_KEY]" in stored_mem.content

    adapter.close()


def test_sanitize_empty_string_and_clean_record_passthrough() -> None:
    """Empty strings return quickly and clean records return unmodified instance."""
    assert redact_secrets("").cleaned_text == ""
    assert redact_secrets("").secrets_detected == 0

    validator = PrePersistenceValidator()
    rec = _make_mem("m_clean", "ns_clean", "Clean harmless text")
    sanitized = validator.sanitize_memory_record(rec)
    assert sanitized is rec  # Identity equality when unchanged


def test_sanitize_nested_mappings_and_lists() -> None:
    """Recursively sanitizes nested dictionaries and lists in metadata."""
    validator = PrePersistenceValidator()
    api_key = "sk-live1234567890abcdef1234567890"
    bearer_token = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature"
    data = {
        "deep": {"nested_key": bearer_token},
        "items": [f"Config: {api_key}", 42, True, None],
        "count": 100,
    }
    cleaned = validator.sanitize_mapping(data, namespace_id="ns")
    assert "[REDACTED_BEARER_TOKEN]" in cleaned["deep"]["nested_key"]
    assert "[REDACTED_API_KEY]" in cleaned["items"][0]
    assert api_key not in cleaned["items"][0]
    assert cleaned["items"][1] == 42
    assert cleaned["count"] == 100
