#!/usr/bin/env python3
"""Generate a verified private pi-acp adapter; never install or launch it.

The callable derive/replace_once/dependency facades remain available. The
versioned backend, transforms and fingerprints live in pi_acp_compat only.
"""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pi_acp_compat import (CompatibilityError, SOURCE, backend, dependency, derive,
                           derive_directory, replace_once)

# Historic fingerprint export; reading code-owned metadata starts no process.
SHA256 = backend('worker')['source_sha256']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    derive_directory(args.output)
    print(backend('worker')['derived_sha256']['worker'])
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError):
        print('Pi ACP derivation failed: pinned compatibility contract rejected', file=sys.stderr)
        raise SystemExit(1)
