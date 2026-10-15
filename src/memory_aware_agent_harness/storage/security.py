"""Pre-persistence validation and credential redaction security boundary.

Establishes a fail-closed defense-in-depth boundary before data reaches SQLite WAL storage:
- Validates provenance: prohibits unauthorized SYSTEM_PROVIDED elevation, falls back to
  lowest-trust EXTERNAL_RETRIEVAL, and rejects malformed origin classifications.
- Redacts known API keys, private keys, bearer tokens, and credentials.
- In fail-closed mode, rejects writes containing detected credentials without persistence.
- Guarantees zero leakage: rejected sensitive content is never included in exception
  diagnostics or log records.

LIMITATION NOTICE:
No general-purpose regular expression pipeline can guarantee the detection and removal of
every arbitrary secret, high-entropy token, or steganographic payload. This module provides
deterministic pre-persistence sanitation against known credential schemas.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from memory_aware_agent_harness.contracts import (
    FrozenDict,
    MemoryOrigin,
    MemoryRecord,
    PrePersistenceSecurityError,
)

logger = logging.getLogger(__name__)

# Pre-compiled high-speed regex patterns for known secret signatures
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "PRIVATE_KEY",
        re.compile(
            r"-----BEGIN (?:[A-Z ]*?)PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z ]*?)PRIVATE KEY-----",
            re.MULTILINE,
        ),
        "[REDACTED_PRIVATE_KEY]",
    ),
    (
        "OPENAI_OR_PROVIDER_KEY",
        re.compile(r"\b(?:sk|org|ant)-(?:live|test|proj)?[A-Za-z0-9_-]{20,}\b"),
        "[REDACTED_API_KEY]",
    ),
    (
        "GITHUB_TOKEN",
        re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{36,}\b"),
        "[REDACTED_GITHUB_TOKEN]",
    ),
    (
        "AWS_ACCESS_KEY_ID",
        re.compile(r"\b(?:AKIA|ASIA|AROA)[0-9A-Z]{16}\b"),
        "[REDACTED_AWS_KEY]",
    ),
    (
        "AWS_SECRET_KEY",
        re.compile(
            r"(?i)\b(?:aws_secret_access_key|aws_secret_key)\s*[:=]\s*['\"]?([A-Za-z0-9/+=]{40})['\"]?"
        ),
        "[REDACTED_AWS_SECRET]",
    ),
    (
        "BEARER_TOKEN",
        re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-\.~+/]{20,}=*\b"),
        "[REDACTED_BEARER_TOKEN]",
    ),
    (
        "GENERIC_CREDENTIAL_ASSIGNMENT",
        re.compile(
            r"(?i)\b(?:password|passwd|secret_key|api_key|client_secret)\s*[:=]\s*['\"]([^'\"]{8,})['\"]"
        ),
        "[REDACTED_CREDENTIAL]",
    ),
)


@dataclass(frozen=True)
class RedactionResult:
    """Outcome of string sanitation."""

    cleaned_text: str
    secrets_detected: int
    matched_rules: tuple[str, ...]


def redact_secrets(text: str) -> RedactionResult:
    """Scan and redact known credential signatures from a string.

    Args:
        text: Untrusted string to sanitize.

    Returns:
        RedactionResult containing sanitized text and detection metadata.
    """
    if not text:
        return RedactionResult(cleaned_text=text, secrets_detected=0, matched_rules=())

    cleaned = text
    detected_count = 0
    matched_rules: list[str] = []

    for rule_name, pattern, placeholder in _SECRET_PATTERNS:
        matches = list(pattern.finditer(cleaned))
        if matches:
            detected_count += len(matches)
            matched_rules.append(rule_name)
            cleaned = pattern.sub(placeholder, cleaned)

    return RedactionResult(
        cleaned_text=cleaned,
        secrets_detected=detected_count,
        matched_rules=tuple(matched_rules),
    )


class PrePersistenceValidator:
    """Validates records and applies redaction policies prior to durable SQLite persistence."""

    def __init__(self, *, fail_closed: bool = False) -> None:
        """Initialize validator.

        Args:
            fail_closed: When True, encountering any secret raises PrePersistenceSecurityError
                         instead of silently redacting.
        """
        self.fail_closed = fail_closed

    def validate_provenance(
        self,
        origin: Any,
        *,
        namespace_id: str,
        record_id: str | None = None,
        is_system_actor: bool = False,
    ) -> MemoryOrigin:
        """Validate provenance origin classification.

        Enforces:
        - Never assign SYSTEM_PROVIDED merely because origin was omitted.
        - Fall back closed to lowest-trust classification (EXTERNAL_RETRIEVAL) if omitted.
        - Reject malformed or unrecognized origin strings.
        - Prohibit external unauthenticated actors from claiming SYSTEM_PROVIDED.
        """
        if origin is None:
            # Fall back closed to lowest-trust classification
            return MemoryOrigin.EXTERNAL_RETRIEVAL

        if isinstance(origin, str):
            try:
                origin = MemoryOrigin(origin.lower())
            except ValueError as exc:
                raise PrePersistenceSecurityError(
                    f"Malformed origin classification for record '{record_id}': invalid value",
                    rule_name="MALFORMED_PROVENANCE_ORIGIN",
                    namespace_id=namespace_id,
                    record_id=record_id,
                ) from exc

        if not isinstance(origin, MemoryOrigin):
            raise PrePersistenceSecurityError(
                f"Malformed origin classification for record '{record_id}': expected MemoryOrigin",
                rule_name="MALFORMED_PROVENANCE_ORIGIN",
                namespace_id=namespace_id,
                record_id=record_id,
            )

        if origin == MemoryOrigin.SYSTEM_PROVIDED and not is_system_actor:
            raise PrePersistenceSecurityError(
                f"Unauthorized SYSTEM_PROVIDED provenance claimed for record '{record_id}'",
                rule_name="UNAUTHORIZED_SYSTEM_PROVENANCE",
                namespace_id=namespace_id,
                record_id=record_id,
            )

        return origin

    def sanitize_text(
        self,
        text: str,
        *,
        namespace_id: str,
        field_name: str = "content",
        record_id: str | None = None,
    ) -> str:
        """Sanitize a text field, enforcing fail-closed boundaries if configured."""
        result = redact_secrets(text)
        if result.secrets_detected > 0:
            if self.fail_closed:
                # IMPORTANT: Never include the detected secret in the error or logs!
                raise PrePersistenceSecurityError(
                    f"Pre-persistence security check failed: "
                    f"credentials detected in '{field_name}' under rules {result.matched_rules}",
                    rule_name="CREDENTIAL_REDACTION_VIOLATION",
                    namespace_id=namespace_id,
                    record_id=record_id,
                    details={"matched_rules": list(result.matched_rules)},
                )
            logger.warning(
                "Redacted %d secret(s) in field '%s' under rules %s (record: %s, ns: %s)",
                result.secrets_detected,
                field_name,
                result.matched_rules,
                record_id,
                namespace_id,
            )
        return result.cleaned_text

    def sanitize_mapping(
        self,
        mapping: Mapping[str, Any],
        *,
        namespace_id: str,
        record_id: str | None = None,
    ) -> dict[str, Any]:
        """Recursively scan and sanitize string values inside a dictionary."""
        sanitized: dict[str, Any] = {}
        for k, v in mapping.items():
            if isinstance(v, str):
                sanitized[k] = self.sanitize_text(
                    v,
                    namespace_id=namespace_id,
                    field_name=f"metadata.{k}",
                    record_id=record_id,
                )
            elif isinstance(v, Mapping):
                sanitized[k] = self.sanitize_mapping(
                    v,
                    namespace_id=namespace_id,
                    record_id=record_id,
                )
            elif isinstance(v, tuple | list):
                sanitized_list: list[Any] = []
                for item in v:
                    if isinstance(item, str):
                        sanitized_list.append(
                            self.sanitize_text(
                                item,
                                namespace_id=namespace_id,
                                field_name=f"metadata.{k}[]",
                                record_id=record_id,
                            )
                        )
                    else:
                        sanitized_list.append(item)
                sanitized[k] = (
                    tuple(sanitized_list) if isinstance(v, tuple) else list(sanitized_list)
                )
            else:
                sanitized[k] = v
        return sanitized

    def sanitize_memory_record(
        self,
        record: MemoryRecord,
        *,
        is_system_actor: bool = False,
    ) -> MemoryRecord:
        """Apply provenance validation and credential redaction to a MemoryRecord.

        Returns a sanitized, immutable MemoryRecord.
        """
        ns_str = str(record.namespace_id)
        rec_id_str = str(record.id)

        # 1. Validate provenance
        validated_origin = self.validate_provenance(
            record.origin,
            namespace_id=ns_str,
            record_id=rec_id_str,
            is_system_actor=is_system_actor,
        )

        # 2. Sanitize main text content
        clean_content = self.sanitize_text(
            record.content,
            namespace_id=ns_str,
            field_name="content",
            record_id=rec_id_str,
        )

        # 3. Sanitize metadata
        clean_metadata = self.sanitize_mapping(
            record.metadata,
            namespace_id=ns_str,
            record_id=rec_id_str,
        )

        # If nothing changed, return original
        if (
            clean_content == record.content
            and validated_origin == record.origin
            and clean_metadata == dict(record.metadata)
        ):
            return record

        return MemoryRecord(
            id=record.id,
            namespace_id=record.namespace_id,
            content=clean_content,
            kind=record.kind,
            state=record.state,
            confidence=record.confidence,
            salience=record.salience,
            created_at=record.created_at,
            updated_at=record.updated_at,
            expires_at=record.expires_at,
            source_references=record.source_references,
            metadata=FrozenDict(clean_metadata),
            access_count=record.access_count,
            version=record.version,
            origin=validated_origin,
            run_id=record.run_id,
            branch_id=record.branch_id,
        )
