"""SQLite persistence. One connection behind a lock; the data is tiny and writes are infrequent."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS models (
  id TEXT PRIMARY KEY, provider TEXT NOT NULL, tag TEXT NOT NULL, base_url TEXT,
  api_key_env TEXT, family TEXT NOT NULL, license TEXT NOT NULL, open_weight INTEGER NOT NULL,
  status TEXT NOT NULL, status_detail TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'api',
  locality TEXT NOT NULL DEFAULT 'local'
);
CREATE TABLE IF NOT EXISTS profile_cells (
  model_id TEXT NOT NULL, pack_id TEXT NOT NULL, category TEXT NOT NULL,
  n INTEGER NOT NULL, correct INTEGER NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (model_id, pack_id, category)
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, request TEXT NOT NULL, created_at TEXT NOT NULL,
  status TEXT NOT NULL, final TEXT
);
CREATE TABLE IF NOT EXISTS events (
  stream_id TEXT NOT NULL, seq INTEGER NOT NULL, event TEXT NOT NULL, data TEXT NOT NULL,
  PRIMARY KEY (stream_id, seq)
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0,
  total INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '', result TEXT, error TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eval_results (
  id TEXT PRIMARY KEY, pack TEXT NOT NULL, created_at TEXT NOT NULL, body TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)
            cols = {r["name"] for r in self._db.execute("PRAGMA table_info(models)")}
            if "locality" not in cols:  # databases created before contract v1.1
                self._db.execute("ALTER TABLE models ADD COLUMN locality TEXT NOT NULL DEFAULT 'local'")
            self._db.commit()

    def _exec(self, sql: str, args: Iterable | dict = ()) -> None:
        with self._lock:
            self._db.execute(sql, args if isinstance(args, dict) else tuple(args))
            self._db.commit()

    def _all(self, sql: str, args: Iterable = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, tuple(args)).fetchall()

    def _one(self, sql: str, args: Iterable = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(sql, tuple(args)).fetchone()

    # ------------------------------------------------------------------ models

    def upsert_model(self, m: dict) -> None:
        self._exec(
            """INSERT INTO models (id, provider, tag, base_url, api_key_env, family, license,
                                   open_weight, status, status_detail, source, locality)
               VALUES (:id, :provider, :tag, :base_url, :api_key_env, :family, :license,
                       :open_weight, :status, :status_detail, :source, :locality)
               ON CONFLICT(id) DO UPDATE SET base_url=excluded.base_url,
                 api_key_env=excluded.api_key_env, family=excluded.family, license=excluded.license,
                 open_weight=excluded.open_weight, status=excluded.status,
                 status_detail=excluded.status_detail, locality=excluded.locality""",
            {**m, "open_weight": int(m["open_weight"])},
        )

    def list_models(self) -> list[dict]:
        return [self._model(r) for r in self._all("SELECT * FROM models ORDER BY id")]

    def get_model(self, model_id: str) -> dict | None:
        r = self._one("SELECT * FROM models WHERE id = ?", (model_id,))
        return self._model(r) if r else None

    def set_model_status(self, model_id: str, status: str, detail: str = "") -> None:
        self._exec("UPDATE models SET status=?, status_detail=? WHERE id=?",
                   (status, detail, model_id))

    def delete_model(self, model_id: str) -> None:
        self._exec("DELETE FROM models WHERE id = ?", (model_id,))
        self._exec("DELETE FROM profile_cells WHERE model_id = ?", (model_id,))

    @staticmethod
    def _model(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["open_weight"] = bool(d["open_weight"])
        return d

    # ------------------------------------------------------------------ profiles

    def set_profile_cell(self, model_id: str, pack_id: str, category: str, n: int, correct: int,
                         when: str) -> None:
        self._exec(
            """INSERT INTO profile_cells VALUES (?,?,?,?,?,?)
               ON CONFLICT(model_id, pack_id, category) DO UPDATE SET n=excluded.n,
                 correct=excluded.correct, updated_at=excluded.updated_at""",
            (model_id, pack_id, category, n, correct, when),
        )

    def clear_profile(self, model_id: str, pack_id: str) -> None:
        self._exec("DELETE FROM profile_cells WHERE model_id=? AND pack_id=?", (model_id, pack_id))

    def profile_cells(self) -> list[dict]:
        return [dict(r) for r in self._all("SELECT * FROM profile_cells ORDER BY model_id")]

    # ------------------------------------------------------------------ streams (runs, jobs)

    def append_event(self, stream_id: str, seq: int, event: str, data: dict) -> None:
        self._exec("INSERT OR REPLACE INTO events VALUES (?,?,?,?)",
                   (stream_id, seq, event, json.dumps(data)))

    def events(self, stream_id: str) -> list[dict]:
        return [{"seq": r["seq"], "event": r["event"], "data": json.loads(r["data"])}
                for r in self._all("SELECT * FROM events WHERE stream_id=? ORDER BY seq",
                                   (stream_id,))]

    # ------------------------------------------------------------------ runs

    def create_run(self, run_id: str, request: dict, created_at: str) -> None:
        self._exec("INSERT INTO runs VALUES (?,?,?,?,NULL)",
                   (run_id, json.dumps(request), created_at, "running"))

    def finish_run(self, run_id: str, status: str, final: dict | None) -> None:
        self._exec("UPDATE runs SET status=?, final=? WHERE id=?",
                   (status, json.dumps(final) if final else None, run_id))

    def get_run(self, run_id: str) -> dict | None:
        r = self._one("SELECT * FROM runs WHERE id=?", (run_id,))
        if not r:
            return None
        return {"id": r["id"], "request": json.loads(r["request"]), "created_at": r["created_at"],
                "status": r["status"], "final": json.loads(r["final"]) if r["final"] else None}

    def fail_orphaned_runs(self) -> None:
        """Runs left 'running' by a previous process can never finish."""
        self._exec("UPDATE runs SET status='error' WHERE status='running'")
        self._exec("UPDATE jobs SET status='error', error='server restarted' "
                   "WHERE status IN ('queued','running')")

    # ------------------------------------------------------------------ jobs

    def create_job(self, job_id: str, kind: str, created_at: str, total: int = 0) -> None:
        self._exec("INSERT INTO jobs (id, kind, status, total, created_at) VALUES (?,?,?,?,?)",
                   (job_id, kind, "queued", total, created_at))

    def update_job(self, job_id: str, **fields) -> None:
        if "result" in fields and fields["result"] is not None:
            fields["result"] = json.dumps(fields["result"])
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))

    def get_job(self, job_id: str) -> dict | None:
        r = self._one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not r:
            return None
        d = dict(r)
        d["result"] = json.loads(d["result"]) if d["result"] else None
        return d

    # ------------------------------------------------------------------ eval

    def save_eval(self, eval_id: str, pack: str, created_at: str, body: dict) -> None:
        self._exec("INSERT INTO eval_results VALUES (?,?,?,?)",
                   (eval_id, pack, created_at, json.dumps(body)))

    def latest_eval(self, pack: str | None = None) -> dict | None:
        sql = "SELECT body FROM eval_results" + (" WHERE pack=?" if pack else "")
        r = self._one(sql + " ORDER BY created_at DESC LIMIT 1", (pack,) if pack else ())
        return json.loads(r["body"]) if r else None
