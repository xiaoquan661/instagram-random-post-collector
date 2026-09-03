"""Backward-compatible import and module entry point."""

from .webui.server import *
from .webui.server import main

if __name__ == "__main__":
    raise SystemExit(main())
