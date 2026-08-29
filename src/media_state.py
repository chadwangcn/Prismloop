"""Durable, single-process state and lease store for MediaRun workers."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional


class MediaRunStore:
    """SQLite-backed source of truth for asynchronous media runs.

    SQLite is deliberately limited to one service process in v1.  The schema
    keeps leases explicit, so a later Postgres implementation can retain the
    same service-facing behaviour.
    """

    def __init__(self, database_path: str) -> None:
        self.database_path = database_path
        if database_path != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def create_run(self, run_id: str, request: Mapping[str, Any], result: Mapping[str, Any]) -> str:
        """Insert a queued run, or return the existing idempotent run id."""
        now = time.time()
        request_json = self._encode(request)
        result_json = self._encode(result)
        idempotency_key = str(request["idempotency_key"])
        environment_ref = str(request["environment_ref"])
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT run_id FROM idempotency_keys WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if row is not None:
                return str(row["run_id"])
            connection.execute(
                """INSERT INTO media_runs
                   (run_id, request_json, result_json, environment_ref, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'queued', ?, ?)""",
                (run_id, request_json, result_json, environment_ref, now, now),
            )
            connection.execute(
                "INSERT INTO idempotency_keys (idempotency_key, run_id, created_at) VALUES (?, ?, ?)",
                (idempotency_key, run_id, now),
            )
            self._append_event_locked(connection, run_id, "accepted", {"environment_ref": environment_ref})
        return run_id

    def get_result(self, run_id: str) -> Dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT result_json FROM media_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"media run not found: {run_id}")
        return json.loads(str(row["result_json"]))

    def get_request(self, run_id: str) -> Dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT request_json FROM media_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"media run not found: {run_id}")
        return json.loads(str(row["request_json"]))

    def claim_next(self, worker_id: str, lease_seconds: int) -> Optional[str]:
        """Claim one queued run whose environment lease is free or expired."""
        now = time.time()
        expires_at = now + lease_seconds
        with self._transaction() as connection:
            rows = connection.execute(
                """SELECT run_id, environment_ref FROM media_runs
                   WHERE status = 'queued'
                   ORDER BY created_at ASC"""
            ).fetchall()
            for row in rows:
                run_id = str(row["run_id"])
                environment_ref = str(row["environment_ref"])
                lease = connection.execute(
                    "SELECT run_id, expires_at FROM device_leases WHERE environment_ref = ?", (environment_ref,)
                ).fetchone()
                if lease is not None and float(lease["expires_at"]) > now:
                    continue
                connection.execute(
                    """INSERT INTO device_leases (environment_ref, run_id, worker_id, expires_at, updated_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(environment_ref) DO UPDATE SET
                         run_id = excluded.run_id, worker_id = excluded.worker_id,
                         expires_at = excluded.expires_at, updated_at = excluded.updated_at""",
                    (environment_ref, run_id, worker_id, expires_at, now),
                )
                cursor = connection.execute(
                    "UPDATE media_runs SET status = 'running', updated_at = ? WHERE run_id = ? AND status = 'queued'",
                    (now, run_id),
                )
                if cursor.rowcount != 1:
                    continue
                self._append_event_locked(connection, run_id, "claimed", {"worker_id": worker_id, "lease_seconds": lease_seconds})
                return run_id
            return None

    def heartbeat(self, run_id: str, lease_seconds: int) -> bool:
        now = time.time()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT environment_ref FROM media_runs WHERE run_id = ? AND status = 'running'", (run_id,)
            ).fetchone()
            if row is None:
                return False
            changed = connection.execute(
                "UPDATE device_leases SET expires_at = ?, updated_at = ? WHERE environment_ref = ? AND run_id = ?",
                (now + lease_seconds, now, str(row["environment_ref"]), run_id),
            ).rowcount
            return bool(changed)

    def complete(self, run_id: str, result: Mapping[str, Any], event_type: str) -> None:
        status = str(result["status"])
        if status not in {"completed", "capability_unavailable", "error", "canceled"}:
            raise ValueError(f"invalid terminal media run status: {status}")
        now = time.time()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT environment_ref, status FROM media_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"media run not found: {run_id}")
            if str(row["status"]) == "canceled":
                return
            if str(row["status"]) != "running":
                raise RuntimeError(f"cannot complete non-running run: {run_id}")
            connection.execute(
                "UPDATE media_runs SET status = ?, result_json = ?, updated_at = ? WHERE run_id = ?",
                (status, self._encode(result), now, run_id),
            )
            connection.execute(
                "DELETE FROM device_leases WHERE environment_ref = ? AND run_id = ?",
                (str(row["environment_ref"]), run_id),
            )
            self._append_event_locked(connection, run_id, event_type, {"status": status})

    def cancel(self, run_id: str, reason: str) -> Dict[str, Any]:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT status, result_json, environment_ref FROM media_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"media run not found: {run_id}")
            if str(row["status"]) not in {"queued", "running"}:
                raise RuntimeError(f"cannot cancel terminal run: {run_id}")
            result = json.loads(str(row["result_json"]))
            result["status"] = "canceled"
            result["error"] = reason
            now = time.time()
            connection.execute(
                "UPDATE media_runs SET status = 'canceled', result_json = ?, updated_at = ? WHERE run_id = ?",
                (self._encode(result), now, run_id),
            )
            connection.execute(
                "DELETE FROM device_leases WHERE environment_ref = ? AND run_id = ?",
                (str(row["environment_ref"]), run_id),
            )
            self._append_event_locked(connection, run_id, "canceled", {"reason": reason})
            return result

    def recover_expired(self) -> Iterable[str]:
        """Terminally mark abandoned in-flight work; queued work remains queued."""
        now = time.time()
        recovered = []
        with self._transaction() as connection:
            rows = connection.execute(
                """SELECT r.run_id, r.result_json, r.environment_ref
                   FROM media_runs r JOIN device_leases l ON l.run_id = r.run_id
                   WHERE r.status = 'running' AND l.expires_at <= ?""",
                (now,),
            ).fetchall()
            for row in rows:
                result = json.loads(str(row["result_json"]))
                result["status"] = "error"
                result["error"] = "worker_lease_expired"
                run_id = str(row["run_id"])
                connection.execute(
                    "UPDATE media_runs SET status = 'error', result_json = ?, updated_at = ? WHERE run_id = ?",
                    (self._encode(result), now, run_id),
                )
                connection.execute(
                    "DELETE FROM device_leases WHERE environment_ref = ? AND run_id = ?",
                    (str(row["environment_ref"]), run_id),
                )
                self._append_event_locked(connection, run_id, "lease_expired", {})
                recovered.append(run_id)
        return recovered

    def events(self, run_id: str) -> list[Dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT event_type, detail_json, created_at FROM run_events WHERE run_id = ? ORDER BY event_id",
                (run_id,),
            ).fetchall()
        return [
            {"event_type": str(row["event_type"]), "detail": json.loads(str(row["detail_json"])), "created_at": row["created_at"]}
            for row in rows
        ]

    def _create_schema(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS media_runs (
                  run_id TEXT PRIMARY KEY,
                  request_json TEXT NOT NULL,
                  result_json TEXT NOT NULL,
                  environment_ref TEXT NOT NULL,
                  status TEXT NOT NULL,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                  idempotency_key TEXT PRIMARY KEY,
                  run_id TEXT NOT NULL UNIQUE REFERENCES media_runs(run_id),
                  created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS device_leases (
                  environment_ref TEXT PRIMARY KEY,
                  run_id TEXT NOT NULL UNIQUE REFERENCES media_runs(run_id),
                  worker_id TEXT NOT NULL,
                  expires_at REAL NOT NULL,
                  updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS run_events (
                  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  run_id TEXT NOT NULL REFERENCES media_runs(run_id),
                  event_type TEXT NOT NULL,
                  detail_json TEXT NOT NULL,
                  created_at REAL NOT NULL
                );
                """
            )
            self._connection.commit()

    def _append_event_locked(self, connection: sqlite3.Connection, run_id: str, event_type: str, detail: Mapping[str, Any]) -> None:
        connection.execute(
            "INSERT INTO run_events (run_id, event_type, detail_json, created_at) VALUES (?, ?, ?, ?)",
            (run_id, event_type, self._encode(detail), time.time()),
        )

    def _transaction(self):
        return _SqliteTransaction(self._lock, self._connection)

    @staticmethod
    def _encode(value: Mapping[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class _SqliteTransaction:
    def __init__(self, lock: threading.RLock, connection: sqlite3.Connection) -> None:
        self._lock = lock
        self._connection = connection

    def __enter__(self) -> sqlite3.Connection:
        self._lock.acquire()
        self._connection.execute("BEGIN IMMEDIATE")
        return self._connection

    def __exit__(self, exc_type, exc, traceback) -> None:
        if exc_type is None:
            self._connection.commit()
        else:
            self._connection.rollback()
        self._lock.release()
