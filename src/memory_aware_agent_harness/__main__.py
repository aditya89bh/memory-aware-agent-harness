"""Entrypoint for running memory_aware_agent_harness as a module."""

import sys

from memory_aware_agent_harness.cli import main

if __name__ == "__main__":
    sys.exit(main())
