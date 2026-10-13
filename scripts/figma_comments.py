#!/usr/bin/env python3
"""Entry point: python3 scripts/figma_comments.py <command> ... (works from any cwd)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fclib.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
