#!/usr/bin/env python3
"""
status.py — show what is currently staged in ~/.koinon-mr-reviews/pending/.

Read-only. Useful for checking whether a given pending package still needs
analysis ("draft") or is ready for publish ("ready"). Does not touch GitLab,
does not modify any file.
"""

from __future__ import annotations

import json
import sys

from common import PENDING_DIR, is_complete


def main() -> int:
    if not PENDING_DIR.exists() or not any(PENDING_DIR.iterdir()):
        print("No pending packages.")
        return 0

    print(f"Pending packages under {PENDING_DIR}:")
    for pkg in sorted(PENDING_DIR.iterdir()):
        if not pkg.is_dir():
            continue
        analysis_path = pkg / "analysis.json"
        marker = "?"
        if analysis_path.exists():
            try:
                a = json.loads(analysis_path.read_text())
                marker = "ready" if is_complete(a) else "draft"
            except json.JSONDecodeError:
                marker = "invalid"
        print(f"  - MR !{pkg.name}: {marker:7}  ({pkg})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
