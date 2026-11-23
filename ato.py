#!/usr/bin/env python3
"""Public ATO entry selected by the sealed launcher."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ato_cli import main


if __name__ == "__main__":
    raise SystemExit(main())
