# -*- coding: utf-8 -*-
"""Fix duplicate daily/weekly bars: merge `daily`→`日线`, `weekly`→`周线`.

Root cause: historical code paths inserted English freq names (daily/weekly)
while the canonical form is Chinese (日线/周线). This created duplicate records
for the same trading day under two different freq labels.

Affected symbols:
  bars daily:  600519, 399001, 002759, 600000
  bars weekly: 002759, 600000
  system.buckets.bars: same as above
"""
from __future__ import annotations

import sys
from datetime import datetime

from pymongo import MongoClient, UpdateOne

MONGO_URL = "mongodb://127.0.0.1:27017/signals"
DB_NAME = "signals"

client = MongoClient(MONGO_URL)
db = client[DB_NAME]


def merge_freq(coll_name: str, from_freq: str, to_freq: str, dry_run: bool = True) -> dict:
    """Merge `from_freq` docs into `to_freq` for overlapping symbols, then delete `from_freq`."""
    coll = db[coll_name]
    stats = {"merged_dates": 0, "deleted_docs": 0, "renamed_docs": 0, "affected_symbols": []}

    # Find symbols with `from_freq`
    from_symbols = coll.distinct("meta.symbol", {"meta.freq": from_freq})
    to_symbols = set(coll.distinct("meta.symbol", {"meta.freq": to_freq}))

    for sym in from_symbols:
        from_docs = list(coll.find({"meta.symbol": sym, "meta.freq": from_freq}, {"dt": 1}))
        from_dates = {str(d["dt"])[:10] for d in from_docs}
        from_count = len(from_docs)

        if sym in to_symbols:
            # Has both: merge non-overlapping dates
            to_dates = {str(d["dt"])[:10] for d in coll.find({"meta.symbol": sym, "meta.freq": to_freq}, {"dt": 1})}
            new_dates = from_dates - to_dates
            overlap = from_dates & to_dates
            stats["merged_dates"] += len(new_dates)

            if new_dates:
                # Copy unique from_docs to to_freq
                new_docs = [
                    d for d in coll.find({"meta.symbol": sym, "meta.freq": from_freq})
                    if str(d["dt"])[:10] in new_dates
                ]
                for doc in new_docs:
                    doc["meta"]["freq"] = to_freq
                    if not dry_run:
                        coll.insert_one(doc)
                stats["renamed_docs"] += len(new_docs)

            # Delete all from_freq docs
            if not dry_run:
                coll.delete_many({"meta.symbol": sym, "meta.freq": from_freq})
            stats["deleted_docs"] += from_count
            stats["affected_symbols"].append(
                f"{sym}: {from_count} {from_freq}, {overlap=} dup dates → merged +{len(new_dates)}, deleted {from_count}"
            )
        else:
            # Only has from_freq: rename freq
            if not dry_run:
                coll.update_many(
                    {"meta.symbol": sym, "meta.freq": from_freq},
                    {"$set": {"meta.freq": to_freq}},
                )
            stats["renamed_docs"] += from_count
            stats["affected_symbols"].append(
                f"{sym}: {from_count} {from_freq} → renamed to {to_freq} (no overlap)"
            )

    return stats


def merge_system_buckets(from_freq: str, to_freq: str, dry_run: bool = True) -> dict:
    """Same logic for system.buckets.bars (time-series bucket collection)."""
    coll = db["system.buckets.bars"]
    stats = {"merged_dates": 0, "deleted_docs": 0, "renamed_docs": 0, "affected_symbols": []}

    from_symbols = coll.distinct("meta.symbol", {"meta.freq": from_freq})
    to_symbols = set(coll.distinct("meta.symbol", {"meta.freq": to_freq}))

    for sym in from_symbols:
        from_count = coll.count_documents({"meta.symbol": sym, "meta.freq": from_freq})

        if sym in to_symbols:
            # Buckets are harder to merge at the individual bar level.
            # Since both freq variants cover similar ranges, keep `to_freq` and drop `from_freq`.
            if not dry_run:
                coll.delete_many({"meta.symbol": sym, "meta.freq": from_freq})
            stats["deleted_docs"] += from_count
            stats["affected_symbols"].append(
                f"{sym}: {from_count} {from_freq} buckets → deleted (to_freq exists)"
            )
        else:
            if not dry_run:
                coll.update_many(
                    {"meta.symbol": sym, "meta.freq": from_freq},
                    {"$set": {"meta.freq": to_freq}},
                )
            stats["renamed_docs"] += from_count
            stats["affected_symbols"].append(
                f"{sym}: {from_count} {from_freq} buckets → renamed to {to_freq}"
            )

    return stats


def main():
    dry_run = "--apply" not in sys.argv
    label = "DRY RUN" if dry_run else "APPLYING"

    print("=" * 80)
    print(f"FIX DUPLICATE FREQ LABELS — {label}")
    print("=" * 80)

    # 1. bars: daily → 日线
    print("\n--- bars: daily → 日线 ---")
    stats = merge_freq("bars", "daily", "日线", dry_run=dry_run)
    _print_stats(stats)

    # 2. bars: weekly → 周线
    print("\n--- bars: weekly → 周线 ---")
    stats = merge_freq("bars", "weekly", "周线", dry_run=dry_run)
    _print_stats(stats)

    # 3. system.buckets.bars: daily → 日线
    print("\n--- system.buckets.bars: daily → 日线 ---")
    stats = merge_system_buckets("daily", "日线", dry_run=dry_run)
    _print_stats(stats)

    # 4. system.buckets.bars: weekly → 周线
    print("\n--- system.buckets.bars: weekly → 周线 ---")
    stats = merge_system_buckets("weekly", "周线", dry_run=dry_run)
    _print_stats(stats)

    # 5. Check index_bars
    print("\n--- index_bars check ---")
    idx_daily = db["index_bars"].distinct("meta.symbol", {"meta.freq": "daily"})
    idx_weekly = db["index_bars"].distinct("meta.symbol", {"meta.freq": "weekly"})
    if idx_daily:
        print(f"index_bars daily symbols: {idx_daily}")
        stats = merge_freq("index_bars", "daily", "日线", dry_run=dry_run)
        _print_stats(stats)
    else:
        print("index_bars: no daily/weekly duplicates found")

    if idx_weekly:
        print(f"index_bars weekly symbols: {idx_weekly}")
        stats = merge_freq("index_bars", "weekly", "周线", dry_run=dry_run)
        _print_stats(stats)

    if dry_run:
        print("\n" + "=" * 80)
        print("DRY RUN complete. Run with --apply to execute fixes.")
        print("=" * 80)


def _print_stats(stats: dict) -> None:
    for sym_info in stats["affected_symbols"]:
        print(f"  {sym_info}")
    print(f"  Summary: {stats['deleted_docs']} deleted, {stats['renamed_docs']} renamed, {stats['merged_dates']} new dates merged")


if __name__ == "__main__":
    main()
