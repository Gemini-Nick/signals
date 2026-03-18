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
// 每日快照，不同数据源(ths/em/sina)独立存储
db.createCollection("board_ranking");
db.board_ranking.createIndex({ dt: -1, source: 1 });
db.board_ranking.createIndex({ dt: -1, board_name: 1 });
// TTL: 90天自动清理历史排行
db.board_ranking.createIndex({ dt: 1 }, { expireAfterSeconds: 7776000 });

print("✅ board_ranking collection created (TTL: 90d)");

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
// TTL: 90天自动清理
db.concept_ranking.createIndex({ dt: 1 }, { expireAfterSeconds: 7776000 });

print("✅ concept_ranking collection created (TTL: 90d)");

// ── Bar Cache（替代 DiskBarCache，TTL 自动过期）────────────────
db.createCollection("bar_cache");
// TTL: 24h 后自动删除（替代 cleanup_old()）
db.bar_cache.createIndex({ created_at: 1 }, { expireAfterSeconds: 86400 });

print("✅ bar_cache collection created (TTL: 24h)");

print("🐲 MongoDB initialization complete — 隆小侠 ready");
