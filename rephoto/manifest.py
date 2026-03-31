from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Iterable

BATCH_SELECTED = "selected"
BATCH_DOWNLOADED = "downloaded"
BATCH_VERIFIED = "verified"
BATCH_TRASHED = "trashed"
BATCH_PUSHED = "pushed"
BATCH_FAILED = "failed"


class ManifestStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS batches (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              remote_batch_id TEXT NOT NULL UNIQUE,
              category TEXT NOT NULL,
              status TEXT NOT NULL,
              selected_count INTEGER NOT NULL,
              archive_path TEXT,
              extract_dir TEXT,
              bytes_downloaded INTEGER NOT NULL DEFAULT 0,
              error TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS files (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
              relative_path TEXT NOT NULL,
              sha256 TEXT NOT NULL,
              size_bytes INTEGER NOT NULL,
              pushed INTEGER NOT NULL DEFAULT 0,
              UNIQUE(batch_id, relative_path)
            );

            CREATE INDEX IF NOT EXISTS idx_batches_status ON batches(status);
            CREATE INDEX IF NOT EXISTS idx_files_batch ON files(batch_id);
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def ensure_batch(self, remote_batch_id: str, category: str, selected_count: int) -> int:
        row = self.conn.execute(
            "SELECT id FROM batches WHERE remote_batch_id = ?",
            (remote_batch_id,),
        ).fetchone()
        if row:
            return int(row["id"])

        now = self._now()
        cursor = self.conn.execute(
            """
            INSERT INTO batches(remote_batch_id, category, status, selected_count, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (remote_batch_id, category, BATCH_SELECTED, selected_count, now, now),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def update_status(
        self,
        batch_id: int,
        status: str,
        *,
        archive_path: Path | None = None,
        extract_dir: Path | None = None,
        bytes_downloaded: int | None = None,
        error: str | None = None,
    ) -> None:
        batch = self.get_batch(batch_id)
        if batch is None:
            raise ValueError(f"Unknown batch id: {batch_id}")

        self.conn.execute(
            """
            UPDATE batches
            SET status = ?,
                archive_path = ?,
                extract_dir = ?,
                bytes_downloaded = ?,
                error = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                str(archive_path) if archive_path is not None else batch["archive_path"],
                str(extract_dir) if extract_dir is not None else batch["extract_dir"],
                bytes_downloaded if bytes_downloaded is not None else batch["bytes_downloaded"],
                error,
                self._now(),
                batch_id,
            ),
        )
        self.conn.commit()

    def set_failed(self, batch_id: int, error: str) -> None:
        self.update_status(batch_id, BATCH_FAILED, error=error)

    def add_file(self, batch_id: int, relative_path: str, sha256: str, size_bytes: int) -> None:
        self.conn.execute(
            """
            INSERT INTO files(batch_id, relative_path, sha256, size_bytes, pushed)
            VALUES(?, ?, ?, ?, 0)
            ON CONFLICT(batch_id, relative_path)
            DO UPDATE SET
              sha256 = excluded.sha256,
              size_bytes = excluded.size_bytes
            """,
            (batch_id, relative_path, sha256, size_bytes),
        )
        self.conn.commit()

    def list_files(self, batch_id: int, *, only_unpushed: bool = False) -> list[sqlite3.Row]:
        query = "SELECT * FROM files WHERE batch_id = ?"
        if only_unpushed:
            query += " AND pushed = 0"
        query += " ORDER BY id"
        rows = self.conn.execute(query, (batch_id,)).fetchall()
        return list(rows)

    def mark_file_pushed(self, batch_id: int, relative_path: str) -> None:
        self.conn.execute(
            "UPDATE files SET pushed = 1 WHERE batch_id = ? AND relative_path = ?",
            (batch_id, relative_path),
        )
        self.conn.commit()

    def count_unpushed_files(self, batch_id: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS count FROM files WHERE batch_id = ? AND pushed = 0",
            (batch_id,),
        ).fetchone()
        return int(row["count"])

    def get_batch(self, batch_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()

    def list_batches(self, statuses: Iterable[str]) -> list[sqlite3.Row]:
        status_values = list(statuses)
        if not status_values:
            return []

        placeholders = ",".join("?" for _ in status_values)
        query = f"SELECT * FROM batches WHERE status IN ({placeholders}) ORDER BY id"
        rows = self.conn.execute(query, status_values).fetchall()
        return list(rows)
