/**
 * 隆小侠 LONG CLAW — MongoDB 初始化脚本
 *
 * Docker entrypoint 自动执行：
 *   deploy/init-mongo.js → /docker-entrypoint-initdb.d/01-init.js
 *
 * 创建: Time-Series Collection + 索引 + TTL 策略
 */

// 切换到 signals 数据库
db = db.getSiblingDB("signals");

// ── K线数据（Time-Series Collection）──────────────────────────
// metaField: {symbol, freq} — 按股票+频率自动分桶
// granularity: "minutes" — 覆盖日线/周线/分钟线所有级别
db.createCollection("bars", {
  timeseries: {
    timeField: "dt",
    metaField: "meta",
    granularity: "minutes"
  }
});

// 复合索引：按 symbol+freq+时间倒序 加速查询
db.bars.createIndex({ "meta.symbol": 1, "meta.freq": 1, "dt": -1 });

print("✅ bars (time-series) collection created");

// ── 同步日志 ──────────────────────────────────────────────────
// _id = "module:symbol" 复合键，如 "stock_daily:SH.600519"
// 跟踪每个模块每个标的的最后同步时间
db.createCollection("sync_log");
db.sync_log.createIndex({ module: 1, status: 1 });

print("✅ sync_log collection created");

// ── 行业排行快照 ──────────────────────────────────────────────
// 历史收盘 canonical；不同实时源写 board_em/board_ths/board_sina
db.createCollection("board_ranking");
db.board_ranking.createIndex({ dt: -1, source: 1 });
db.board_ranking.createIndex({ dt: -1, board_name: 1 });

print("✅ board_ranking canonical collection created (no TTL)");

// ── 行业成分股 ────────────────────────────────────────────────
// _id = board_name，如 "半导体"
// symbols: ["SZ.002049", "SH.688981", ...]
db.createCollection("board_constituents");
// TTL: 30天未更新自动清理
db.board_constituents.createIndex(
  { updated_at: 1 },
  { expireAfterSeconds: 2592000 }
);

print("✅ board_constituents collection created (TTL: 30d)");

// ── 概念排行快照 ──────────────────────────────────────────────
db.createCollection("concept_ranking");
db.concept_ranking.createIndex({ dt: -1, source: 1 });

print("✅ concept_ranking canonical collection created (no TTL)");

// ── 实时源级快照（短 TTL，不作为历史事实源）──────────────────
[
  "board_em", "board_ths", "board_sina",
  "concept_em", "concept_ths", "concept_sina"
].forEach(function(name) {
  db.createCollection(name);
  db[name].createIndex({ dt: -1 });
  db[name].createIndex({ dt: -1, board_name: 1 });
  db[name].createIndex({ dt: 1 }, { expireAfterSeconds: 604800 });
  print("✅ " + name + " source snapshot collection created (TTL: 7d)");
});

// ── Provider/Data health metadata ───────────────────────────
db.createCollection("provider_health");
db.provider_health.createIndex(
  { provider: 1, endpoint: 1, domain: 1 },
  { unique: true }
);
db.provider_health.createIndex({ status: 1, last_success_at: -1 });
print("✅ provider_health collection created");

db.createCollection("data_freshness");
db.data_freshness.createIndex(
  { domain: 1, market: 1, mode: 1, collection: 1 },
  { unique: true }
);
db.data_freshness.createIndex({ latest_dt: -1 });
print("✅ data_freshness collection created");

// ── 运行时缓存集合 ───────────────────────────────────────────
db.createCollection("stock_names");
db.stock_names.createIndex({ code: 1 });
db.stock_names.createIndex({ name: 1 });
db.stock_names.createIndex({ updated_at: 1 }, { expireAfterSeconds: 2592000 });
print("✅ stock_names collection created (TTL: 30d)");

db.createCollection("concept_constituents");
db.concept_constituents.createIndex({ concept_name: 1 });
db.concept_constituents.createIndex({ updated_at: 1 }, { expireAfterSeconds: 2592000 });
print("✅ concept_constituents collection created (TTL: 30d)");

[
  "social_comment", "social_weibo", "social_heat"
].forEach(function(name) {
  db.createCollection(name);
  db[name].createIndex({ dt: -1 });
  db[name].createIndex({ updated_at: 1 }, { expireAfterSeconds: 604800 });
  print("✅ " + name + " collection created (TTL: 7d)");
});

db.createCollection("index_bars");
db.index_bars.createIndex({ "meta.symbol": 1, "meta.freq": 1, "dt": -1 });
print("✅ index_bars collection created");

db.createCollection("quote_snapshots");
db.quote_snapshots.createIndex({ symbol: 1, dt: -1 });
db.quote_snapshots.createIndex({ dt: 1 }, { expireAfterSeconds: 259200 });
print("✅ quote_snapshots collection created (TTL: 3d)");

db.createCollection("market_pools");
db.market_pools.createIndex({ pool: 1, dt: -1 });
db.market_pools.createIndex({ updated_at: 1 }, { expireAfterSeconds: 604800 });
print("✅ market_pools collection created (TTL: 7d)");

db.createCollection("rotation_history");
db.rotation_history.createIndex({ dt: -1 });
print("✅ rotation_history collection created");

db.createCollection("cluster_history");
db.cluster_history.createIndex({ dt: -1 });
print("✅ cluster_history collection created");

db.createCollection("refresh_requests");
db.refresh_requests.createIndex({ status: 1, created_at: -1 });
db.refresh_requests.createIndex({ created_at: 1 }, { expireAfterSeconds: 604800 });
print("✅ refresh_requests collection created (TTL: 7d)");

// ── Bar Cache（替代 DiskBarCache，TTL 自动过期）────────────────
db.createCollection("bar_cache");
// TTL: 24h 后自动删除（替代 cleanup_old()）
db.bar_cache.createIndex({ created_at: 1 }, { expireAfterSeconds: 86400 });

print("✅ bar_cache collection created (TTL: 24h)");

// ── 信号记录（迁移自 SQLite backtest.db）──────────────────────
db.createCollection("signals");
db.signals.createIndex(
  { symbol: 1, signal_date: 1, signal_type: 1, freq: 1 },
  { unique: true }
);
db.signals.createIndex({ evaluated: 1, signal_date: 1 });
db.signals.createIndex({ signal_type: 1, freq: 1 });

print("✅ signals collection created");

// ── 交易配对 ──────────────────────────────────────────────────
db.createCollection("trade_pairs");
db.trade_pairs.createIndex({ symbol: 1, buy_date: 1 });
db.trade_pairs.createIndex(
  { buy_record_id: 1, sell_record_id: 1 },
  { unique: true }
);

print("✅ trade_pairs collection created");

// ── 交易日志（迁移自 SQLite trade_log.db）─────────────────────
db.createCollection("trades");
db.trades.createIndex({ symbol: 1, entry_date: -1 });
db.trades.createIndex({ tags: 1 });

print("✅ trades collection created");

// ── 遗漏信号 ──────────────────────────────────────────────────
db.createCollection("missed_signals");
db.missed_signals.createIndex({ symbol: 1, signal_date: -1 });

print("✅ missed_signals collection created");

// ── 实验日志（迁移自 TSV）─────────────────────────────────────
db.createCollection("experiments");
db.experiments.createIndex({ timestamp: -1 });
db.experiments.createIndex({ decision: 1 });

print("✅ experiments collection created");

print("🐲 MongoDB initialization complete — 隆小侠 ready");
