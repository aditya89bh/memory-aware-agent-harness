"""Packaging and build smoke tests for Phase 0."""

from __future__ import annotations

import importlib
from pathlib import Path


def test_package_can_be_imported() -> None:
    """Verify package can be dynamically imported and has version."""
    pkg = importlib.import_module("memory_aware_agent_harness")
    assert hasattr(pkg, "__version__")
    assert pkg.__version__ == "0.1.0.dev0"


def test_contracts_module_can_be_imported() -> None:
    """Verify contracts submodule exports expected core symbols."""
    contracts = importlib.import_module("memory_aware_agent_harness.contracts")
    assert hasattr(contracts, "RunStatus")
    assert hasattr(contracts, "MemoryKind")
    assert hasattr(contracts, "MemoryRecord")
    assert hasattr(contracts, "ModelPort")
    assert hasattr(contracts, "MemoryStoreProtocol")
    assert hasattr(contracts, "ToolProtocol")


def test_doubles_module_can_be_imported() -> None:
    """Verify doubles submodule exports deterministic test doubles."""
    doubles = importlib.import_module("memory_aware_agent_harness.doubles")
    assert hasattr(doubles, "DeterministicClock")
    assert hasattr(doubles, "DeterministicIdGenerator")


def test_validation_module_can_be_imported() -> None:
    """Verify validation submodule exports validation utilities."""
    val = importlib.import_module("memory_aware_agent_harness.validation")
    assert hasattr(val, "validate_memory_record")
    assert hasattr(val, "validate_external_input")
    assert hasattr(val, "BoundaryValidationError")


def test_py_typed_marker_is_packaged() -> None:
    """Verify py.typed marker file exists in package root for PEP 561 compliance."""
    pkg = importlib.import_module("memory_aware_agent_harness")
    assert pkg.__file__ is not None
    pkg_dir = Path(pkg.__file__).resolve().parent
    py_typed = pkg_dir / "py.typed"
    assert py_typed.is_file(), f"py.typed marker missing at {py_typed}"


def test_pyproject_toml_exists_and_is_valid() -> None:
    """Verify pyproject.toml exists in root and contains expected keys."""
    root_dir = Path(__file__).resolve().parent.parent.parent
    pyproject_path = root_dir / "pyproject.toml"
    assert pyproject_path.is_file()

    content = pyproject_path.read_text(encoding="utf-8")
    assert 'name = "memory-aware-agent-harness"' in content
    assert 'requires-python = ">=3.11"' in content
    assert "[build-system]" in content
