"""Backward-compatible import and module entry point."""

from .collector.cli import *
from .collector.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
