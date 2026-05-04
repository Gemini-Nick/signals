# -*- coding: utf-8 -*-
"""Scan ALL MongoDB collections for duplicate daily bar records."""
from __future__ import annotations

import sys
from collections import defaultdict

from pymongo import MongoClient

MONGO_URL = "mongodb://127.0.0.1:27017/signals"
DB_NAME = "signals"

client = MongoClient(MONGO_URL)
db = client[DB_NAME]

# Key fields per collection for dedup check
COLLECTION_KEYS = {
    "bars": [("meta.symbol", 1), ("meta.freq", 1), ("dt", 1)],
    "index_bars": [("meta.symbol", 1), ("meta.freq", 1), ("dt", 1)],
    "kline_cache": [("code", 1), ("freq", 1), ("dt", 1)],
}

print("=" * 80)
print("SCANNING ALL COLLECTIONS FOR DUPLICATES")
print("=" * 80)

total_duplicates = 0
all_dup_details = {}

for collection_name, key_fields in COLLECTION_KEYS.items():
    coll = db[collection_name]
    total_count = coll.count_documents({})
    print(f"\n--- {collection_name} (total docs: {total_count}) ---")

    # Build group key
    group_id = {}
    for field, _ in key_fields:
        field_clean = field.replace(".", "_")
        group_id[field_clean] = f"${field}"

    pipeline = [
        {"$group": {
            "_id": group_id,
            "count": {"$sum": 1},
            "ids": {"$push": "$_id"},
        }},
        {"$match": {"count": {"$gt": 1}}},
        {"$sort": {"count": -1}},
    ]

    dup_groups = list(coll.aggregate(pipeline))
    dup_count = len(dup_groups)
    dup_docs = sum(g["count"] - 1 for g in dup_groups)  # extra docs

    print(f"  Duplicate groups: {dup_count}")
    print(f"  Extra documents: {dup_docs}")

    if dup_groups:
        all_dup_details[collection_name] = {
            "groups": dup_count,
            "extra_docs": dup_docs,
        }
        # Show top 20
        print(f"  Top duplicates:")
        for g in dup_groups[:20]:
            key = g["_id"]
            cnt = g["count"]
            print(f"    {key} -> {cnt} records ({cnt - 1} extra)")
        if dup_count > 20:
            print(f"    ... and {dup_count - 20} more groups")
        total_duplicates += dup_docs

# Also check for any other collections that might have bar-like data
print(f"\n--- Checking other collections for potential bar-like data ---")
all_colls = db.list_collection_names()
for cname in sorted(all_colls):
    if cname in COLLECTION_KEYS:
        continue
    # Skip known non-bar collections
    skip_patterns = [
        "board_", "concept_", "quote_", "market_", "security_", "chain_",
        "signal", "strategy_", "sync_", "trade_", "provider_", "data_freshness",
        "terminal_", "knowledge_", "minute_", "source_", "fullmarket_",
    ]
    if any(cname.startswith(p) for p in skip_patterns):
        continue
    print(f"  Skipping: {cname} (non-bar collection)")

print("\n" + "=" * 80)
print("SUMMARY")
print("=" * 80)
if total_duplicates == 0:
    print("No duplicates found across all collections.")
else:
    print(f"Total extra documents to remove: {total_duplicates}")
    for cname, detail in all_dup_details.items():
        print(f"  {cname}: {detail['groups']} dup groups, {detail['extra_docs']} extra docs")

client.close()
