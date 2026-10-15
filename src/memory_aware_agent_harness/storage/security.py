"""Pre-persistence validation and credential redaction security boundary.

Establishes a defense-in-depth boundary before data reaches SQLite WAL storage:
- Validates provenance: prohibits unauthorized SYSTEM_PROVIDED elevation, falls back to
  lowest-trust EXTERNAL_RETRIEVAL, and rejects malformed origin classifications.
- By default (fail_closed=False), performs in-place redaction of known API keys, private keys,
  JWT tokens, bearer tokens, and credential assignments before parameter binding.
- In opt-in fail-closed mode (fail_closed=True), rejects writes containing detected credentials
  without persistence by raising PrePersistenceSecurityError.
- When sanitization itself encounters an unrecoverable failure (e.g. non-string keys, invalid
  types, recursion limits), fails closed in all modes.
- Guarantees zero leakage: rejected sensitive content is never included in exception
  diagnostics or log records.

LIMITATION NOTICE:
No general-purpose regular expression pipeline can guarantee the detection and removal of
every arbitrary secret, high-entropy token, or steganographic payload. This module provides
deterministic pre-persistence sanitation against known credential schemas and a small set of
credential-named keys. It is a best-effort control, not comprehensive secret detection.

This boundary only protects FUTURE writes. Redaction does not erase anything that was already
written: SQLite database pages, free pages, and write-ahead-log (WAL) frames that previously held
sensitive content may persist on disk (and in backups) until separately remediated, for example
by rotating the exposed credential, running ``VACUUM`` after a WAL checkpoint, and securely
destroying old database/WAL/backup files. Nothing here guarantees physical erasure.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from memory_aware_agent_harness.contracts import (
    FrozenDict,
    MemoryOrigin,
    MemoryRecord,
    PrePersistenceSecurityError,
)

logger = logging.getLogger(__name__)

_MAX_PAYLOAD_DEPTH = 64

# Mapping keys whose string values are treated as credentials regardless of their format.
_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "client_secret",
        "secret_key",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "auth_token",
        "token",
        "authorization",
        "private_key",
        "credential",
        "credentials",
    }
)


def _is_sensitive_key(key: str) -> bool:
    return key.strip().lower().replace("-", "_") in _SENSITIVE_KEYS


def _looks_like_secret_value(value: str) -> bool:
    """Heuristic: long-enough string that is not already a redaction placeholder."""
    stripped = value.strip()
    return len(stripped) >= 8 and not stripped.startswith("[REDACTED")


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
        re.compile(
            r"(?:\b|(?<=_))(?:sk|org|ant)-(?:live|test|proj)?[A-Za-z0-9_-]{20,}(?:\b|(?=_)|$)"
        ),
        "[REDACTED_API_KEY]",
    ),
    (
        "GITHUB_TOKEN",
        re.compile(
            r"(?:\b|(?<=_))(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{36,}(?:\b|(?=_)|$)"
        ),
        "[REDACTED_GITHUB_TOKEN]",
    ),
    (
        "AWS_ACCESS_KEY_ID",
        re.compile(r"(?:\b|(?<=_))(?:AKIA|ASIA|AROA)[0-9A-Z]{16}(?:\b|(?=_)|$)"),
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
        "JWT_TOKEN",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
        "[REDACTED_JWT_TOKEN]",
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
        if not isinstance(text, str):
            raise PrePersistenceSecurityError(
                f"Non-string value supplied for text field '{field_name}'",
                rule_name="UNSUPPORTED_PAYLOAD_TYPE",
                namespace_id=namespace_id,
                record_id=record_id,
            )
        try:
            result = redact_secrets(text)
        except Exception:
            # Fail closed. Never chain: the original exception may quote the input text.
            raise PrePersistenceSecurityError(
                f"Pre-persistence sanitization failed for '{field_name}'; write rejected",
                rule_name="SANITIZATION_FAILURE",
                namespace_id=namespace_id,
                record_id=record_id,
            ) from None
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
        field_name: str = "metadata",
    ) -> dict[str, Any]:
        """Recursively scan and sanitize every string inside a nested mapping."""
        result = self.sanitize_payload(
            mapping,
            namespace_id=namespace_id,
            field_name=field_name,
            record_id=record_id,
        )
        return dict(result)

    def sanitize_payload(
        self,
        value: Any,
        *,
        namespace_id: str,
        field_name: str,
        record_id: str | None = None,
    ) -> Any:
        """Deeply sanitize an arbitrary JSON-like payload before SQL parameters are built.

        Covers nested mappings (keys and values), lists/tuples/sets, and strings at any depth.
        Fails closed: any unsupported type, excessive nesting, or unexpected sanitizer error
        raises PrePersistenceSecurityError. The raised error never embeds payload content and
        suppresses exception chaining so the offending value cannot leak via tracebacks.
        """
        try:
            return self._sanitize_value(
                value,
                namespace_id=namespace_id,
                field_name=field_name,
                record_id=record_id,
                depth=0,
            )
        except PrePersistenceSecurityError:
            raise
        except Exception:
            # Deliberately omit the original exception: it may quote secret-bearing content.
            raise PrePersistenceSecurityError(
                f"Pre-persistence sanitization failed for '{field_name}'; write rejected",
                rule_name="SANITIZATION_FAILURE",
                namespace_id=namespace_id,
                record_id=record_id,
            ) from None

    def _sanitize_value(
        self,
        value: Any,
        *,
        namespace_id: str,
        field_name: str,
        record_id: str | None,
        depth: int,
    ) -> Any:
        if depth > _MAX_PAYLOAD_DEPTH:
            raise PrePersistenceSecurityError(
                f"Payload nesting in '{field_name}' exceeds the supported depth",
                rule_name="PAYLOAD_TOO_DEEP",
                namespace_id=namespace_id,
                record_id=record_id,
            )

        if value is None or isinstance(value, bool | int | float):
            return value
        if isinstance(value, str):
            return self.sanitize_text(
                value,
                namespace_id=namespace_id,
                field_name=field_name,
                record_id=record_id,
            )
        if isinstance(value, datetime | date):
            return value
        if isinstance(value, Mapping):
            out: dict[Any, Any] = {}
            for k, v in value.items():
                if not isinstance(k, str):
                    raise PrePersistenceSecurityError(
                        f"Non-string mapping key in '{field_name}'",
                        rule_name="UNSUPPORTED_PAYLOAD_TYPE",
                        namespace_id=namespace_id,
                        record_id=record_id,
                    )
                clean_key = self.sanitize_text(
                    k,
                    namespace_id=namespace_id,
                    field_name=f"{field_name}.<key>",
                    record_id=record_id,
                )
                cleaned = self._sanitize_value(
                    v,
                    namespace_id=namespace_id,
                    field_name=field_name,
                    record_id=record_id,
                    depth=depth + 1,
                )
                if (
                    isinstance(v, str)
                    and cleaned == v
                    and _is_sensitive_key(k)
                    and _looks_like_secret_value(v)
                ):
                    cleaned = self._handle_sensitive_key(
                        namespace_id=namespace_id,
                        field_name=field_name,
                        record_id=record_id,
                    )
                out[clean_key] = cleaned
            return out
        if isinstance(value, tuple | list):
            items = [
                self._sanitize_value(
                    item,
                    namespace_id=namespace_id,
                    field_name=f"{field_name}[]",
                    record_id=record_id,
                    depth=depth + 1,
                )
                for item in value
            ]
            return tuple(items) if isinstance(value, tuple) else items

        raise PrePersistenceSecurityError(
            f"Unsupported payload type in '{field_name}'",
            rule_name="UNSUPPORTED_PAYLOAD_TYPE",
            namespace_id=namespace_id,
            record_id=record_id,
        )

    def _handle_sensitive_key(
        self,
        *,
        namespace_id: str,
        field_name: str,
        record_id: str | None,
    ) -> str:
        """Redact (or reject, when fail-closed) a value stored under a credential-named key."""
        if self.fail_closed:
            raise PrePersistenceSecurityError(
                f"Pre-persistence security check failed: credential-named key in '{field_name}'",
                rule_name="CREDENTIAL_KEY_VIOLATION",
                namespace_id=namespace_id,
                record_id=record_id,
            )
        logger.warning(
            "Redacted value under credential-named key in field '%s' (record: %s, ns: %s)",
            field_name,
            record_id,
            namespace_id,
        )
        return "[REDACTED_CREDENTIAL]"

    def sanitize_json_text(
        self,
        text: str | None,
        *,
        namespace_id: str,
        field_name: str,
        record_id: str | None = None,
    ) -> str | None:
        """Sanitize an already-serialized JSON document.

        The document is parsed, sanitized structurally, and re-serialized only when content
        changed, so unaffected payloads are persisted byte-for-byte as produced by the caller.
        Non-JSON text is sanitized as plain text.
        """
        if text is None:
            return None
        try:
            parsed = json.loads(text)
        except ValueError:
            return self.sanitize_text(
                text,
                namespace_id=namespace_id,
                field_name=field_name,
                record_id=record_id,
            )
        cleaned = self.sanitize_payload(
            parsed,
            namespace_id=namespace_id,
            field_name=field_name,
            record_id=record_id,
        )
        if cleaned == parsed:
            return text
        return json.dumps(
            cleaned,
            default=lambda o: dict(o) if isinstance(o, Mapping) else str(o),
            sort_keys=True,
            separators=(",", ":"),
        )

    def sanitize_error_text(
        self,
        text: str | None,
        *,
        namespace_id: str,
        field_name: str = "error",
        record_id: str | None = None,
    ) -> str | None:
        """Sanitize persisted error and exception messages."""
        if text is None:
            return None
        return self.sanitize_text(
            text,
            namespace_id=namespace_id,
            field_name=field_name,
            record_id=record_id,
        )

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


_DEFAULT_VALIDATOR = PrePersistenceValidator()


def resolve_validator(validator: PrePersistenceValidator | None) -> PrePersistenceValidator:
    """Return the supplied validator, or the shared redacting default.

    Every durable write path resolves its validator through this function so that no repository
    function can persist a payload without crossing the pre-persistence boundary.
    """
    return validator if validator is not None else _DEFAULT_VALIDATOR
