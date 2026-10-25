#!/usr/bin/env python3
"""Set the built wheel identity in a release manifest."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print("usage: update_manifest.py MANIFEST WHEEL_FILENAME WHEEL_SHA256", file=sys.stderr)
        return 2
    path = Path(argv[1])
    data = json.loads(path.read_text(encoding="utf-8"))
    data["wheel"] = {"filename": argv[2], "sha256": argv[3]}
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
