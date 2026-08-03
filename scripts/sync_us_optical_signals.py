#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch the bounded US optical-chain universe into MongoDB and scan signals."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from signals.sync.db import get_db
from signals.sync.modules.us_optical_research import run_us_optical_research


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--direct-only",
        action="store_true",
        help="Only AXTI/COHR/LITE/FN/AAOI/CIEN; exclude GLW/AVGO/MRVL context names.",
    )
    args = parser.parse_args()
    result = run_us_optical_research(get_db(), include_context=not args.direct_only)
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    return 0 if result.get("status") == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
