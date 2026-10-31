// 学习资产库 SQLite 存储（c4_asset_learning.md §5）。
// 驱动 node:sqlite（Node 内置，零新依赖；免 flag 可用版本以打包预置 Node
// 二进制实测为准——§5.1）。表结构 §5.2：assets / events / conflicts /
// regression_cases / pack_imports（M2 起使用 conflicts / regression_cases /
// pack_imports，M1 先建全量 schema）。

import { DatabaseSync } from "node:sqlite";
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";

const SCHEMA = `
CREATE TABLE IF NOT EXISTS assets (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  stage TEXT NOT NULL,
  scope TEXT NOT NULL CHECK (scope IN ('global','site')),
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'candidate',
  usage_count INTEGER NOT NULL DEFAULT 0,
  confirm_streak INTEGER NOT NULL DEFAULT 0,
  origin_site TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (kind, stage, scope, key)
);
CREATE INDEX IF NOT EXISTS idx_assets_kind_status ON assets(kind, status);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  stage TEXT NOT NULL,
  session_id TEXT NOT NULL,
  kind TEXT,
  asset_key TEXT,
  source TEXT NOT NULL DEFAULT 'stage',
  input_ref TEXT NOT NULL,
  proposed TEXT NOT NULL,
  final TEXT NOT NULL,
  corrected INTEGER NOT NULL,
  confidence REAL NOT NULL,
  occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conflicts (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  stage TEXT NOT NULL,
  scope TEXT NOT NULL,
  key TEXT NOT NULL,
  existing_value TEXT NOT NULL,
  incoming_value TEXT NOT NULL,
  pack_id INTEGER NOT NULL,
  resolution TEXT,
  created_at TEXT,
  resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS regression_cases (
  id INTEGER PRIMARY KEY,
  stage TEXT NOT NULL,
  input TEXT NOT NULL,
  expect TEXT NOT NULL,
  level TEXT NOT NULL DEFAULT 'deterministic',
  origin TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pack_imports (
  id INTEGER PRIMARY KEY,
  pack_file TEXT NOT NULL,
  pack_version TEXT NOT NULL,
  checksum TEXT NOT NULL,
  imported_at TEXT NOT NULL,
  rollback_snapshot TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'applied'
);
`;

export function open_assets_db(dbPath: string): DatabaseSync {
    mkdirSync(dirname(dbPath), { recursive: true });
    const db = new DatabaseSync(dbPath);
    db.exec("PRAGMA journal_mode = WAL;");
    db.exec(SCHEMA);
    return db;
}
