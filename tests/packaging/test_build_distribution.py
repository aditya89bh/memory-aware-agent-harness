"""Automated validation of package build, isolated wheel, and distribution integrity."""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import memory_aware_agent_harness


def test_package_version_and_metadata() -> None:
    """Verifies top-level package version and required exports."""
    assert hasattr(memory_aware_agent_harness, "__version__")
    assert memory_aware_agent_harness.__version__ == "0.1.0.dev0"


def test_build_sdist_and_wheel_cleanly(tmp_path: Path) -> None:
    """Verifies that python -m build produces valid sdist and wheel in isolated directory."""
    project_root = Path(__file__).resolve().parent.parent.parent
    dist_dir = tmp_path / "dist"

    cmd = [
        sys.executable,
        "-m",
        "build",
        "--sdist",
        "--wheel",
        "--outdir",
        str(dist_dir),
        str(project_root),
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"Build failed:\n{result.stderr}\n{result.stdout}"

        # Verify both artifacts generated
        wheels = list(dist_dir.glob("*.whl"))
        sdists = list(dist_dir.glob("*.tar.gz"))
        assert len(wheels) == 1, f"Expected 1 wheel, found: {wheels}"
        assert len(sdists) == 1, f"Expected 1 sdist, found: {sdists}"

        wheel_path = wheels[0]
        with zipfile.ZipFile(wheel_path) as zf:
            namelist = zf.namelist()

            # 1. Mandatory package files
            assert "memory_aware_agent_harness/__init__.py" in namelist
            assert "memory_aware_agent_harness/py.typed" in namelist
            assert "memory_aware_agent_harness/contracts.py" in namelist
            assert "memory_aware_agent_harness/cli.py" in namelist
            assert "memory_aware_agent_harness/storage/adapter.py" in namelist
            assert "memory_aware_agent_harness/storage/schema.py" in namelist
            assert "memory_aware_agent_harness/runtime/runner.py" in namelist

            # 2. License and metadata
            license_found = any("LICENSE" in name for name in namelist)
            metadata_found = any("METADATA" in name for name in namelist)
            assert license_found, "Wheel missing LICENSE"
            assert metadata_found, "Wheel missing METADATA"

            # 3. Entry points check
            entry_points = [name for name in namelist if name.endswith("entry_points.txt")]
            assert len(entry_points) == 1, "Wheel missing entry_points.txt"
            ep_content = zf.read(entry_points[0]).decode("utf-8")
            assert "memory-agent = memory_aware_agent_harness.cli:main" in ep_content

            # 4. Strict exclusion: tests and examples must NEVER leak into production wheel
            for name in namelist:
                assert not name.startswith("tests/"), f"Leaked test file in wheel: {name}"
                assert not name.startswith("examples/"), f"Leaked example file in wheel: {name}"
    finally:
        build_dir = project_root / "build"
        if build_dir.exists():
            import shutil

            shutil.rmtree(build_dir, ignore_errors=True)
        for egg_info in (project_root / "src").glob("*.egg-info"):
            import shutil

            shutil.rmtree(egg_info, ignore_errors=True)
