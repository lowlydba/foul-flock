#!/usr/bin/env python3
"""ALPR Siting Compliance Map - build pipeline entry point.

The pipeline lives in the foulflock package beside this script; see
foulflock/cli.py for usage and flags, or run: python build/build.py --help
"""

import sys
from pathlib import Path

# build/ is not a package: put it on the path so foulflock and vectortiles
# import the same way here, in tests, and in CI.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from foulflock.cli import main  # noqa: E402

if __name__ == "__main__":
    # CI pipes stdout, which makes Python block-buffer it; line-buffer so
    # progress lines stream live in the Actions log instead of in one burst.
    sys.stdout.reconfigure(line_buffering=True)
    main()