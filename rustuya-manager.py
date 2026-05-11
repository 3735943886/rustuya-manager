#!/usr/bin/env python3
"""Backwards-compatible entry script.

The implementation lives in `src/rustuya_manager/`. Run via either
`python3 rustuya-manager.py ...` or `python -m rustuya_manager ...`.
"""

import os
import sys

# Allow running directly from a fresh checkout without `pip install -e .`
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "src")
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from rustuya_manager.cli import main

if __name__ == "__main__":
    sys.exit(main())
