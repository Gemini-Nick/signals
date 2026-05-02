#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Repair legacy Mongo bars volume units and optionally rebuild weekly bars."""
from __future__ import annotations

import argparse
import json

from signals.data.mongo_fallback import get_db
from signals.sync.modules.weekly_rollup import sync_weekly_rollup
from signals.sync.volume_repair import repair_daily_volume_units


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write Mongo updates; default is dry-run")
    parser.add_argument("--symbols", default="", help="comma-separated symbols to repair, e.g. 002709,SZ.002759")
    parser.add_argument("--rebuild-weekly", action="store_true", help="rebuild weekly bars after daily repair")
    args = parser.parse_args()

    db = get_db()
    if db is None:
        raise SystemExit("MongoDB is not available")
    symbols = [value.strip() for value in args.symbols.replace(";", ",").split(",") if value.strip()] or None
    stats = repair_daily_volume_units(db, symbols=symbols, dry_run=not args.apply)
    result = {"daily_repair": stats}
    if args.rebuild_weekly:
        if not args.apply:
            result["weekly_rollup"] = "skipped_dry_run"
        else:
            result["weekly_rollup"] = sync_weekly_rollup(db)
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
