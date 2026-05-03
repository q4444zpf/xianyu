"""SQLite 持久化已推送的商品 ID，提供增量过滤。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .models import Item


SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_items (
    item_id     TEXT PRIMARY KEY,
    title       TEXT,
    price       TEXT,
    first_seen  TEXT NOT NULL,
    notified    INTEGER NOT NULL DEFAULT 0
);
"""


class SeenItemStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def is_empty(self) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM seen_items").fetchone()
            return row["c"] == 0

    def existing_ids(self, item_ids: Iterable[str]) -> set[str]:
        ids = list(item_ids)
        if not ids:
            return set()
        placeholders = ",".join(["?"] * len(ids))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT item_id FROM seen_items WHERE item_id IN ({placeholders})",
                ids,
            ).fetchall()
            return {r["item_id"] for r in rows}

    def notified_status_for_ids(self, item_ids: list[str]) -> dict[str, int]:
        """返回 item_id -> notified(0/1)，仅查询给定 id。"""
        if not item_ids:
            return {}
        placeholders = ",".join(["?"] * len(item_ids))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT item_id, notified FROM seen_items WHERE item_id IN ({placeholders})",
                item_ids,
            ).fetchall()
        return {str(r["item_id"]): int(r["notified"]) for r in rows}

    def filter_new(self, items: list[Item]) -> list[Item]:
        """返回「待推送」商品：库中不存在，或存在但 notified=0。保持 items 顺序。"""
        if not items:
            return []
        ids = [item.item_id for item in items]
        status = self.notified_status_for_ids(ids)
        out: list[Item] = []
        for item in items:
            st = status.get(item.item_id)
            if st is None:
                out.append(item)
            elif st == 0:
                out.append(item)
        return out

    def upsert_many(self, items: list[Item], notified: bool) -> None:
        if not items:
            return
        rows = [
            (
                item.item_id,
                item.title,
                item.price,
                datetime.now().isoformat(timespec="seconds"),
                1 if notified else 0,
            )
            for item in items
        ]
        with self._conn() as conn:
            conn.executemany(
                """
                INSERT INTO seen_items(item_id, title, price, first_seen, notified)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    title = excluded.title,
                    price = excluded.price,
                    notified = MAX(seen_items.notified, excluded.notified)
                """,
                rows,
            )

    def mark_notified(self, item_ids: Iterable[str]) -> None:
        ids = list(item_ids)
        if not ids:
            return
        placeholders = ",".join(["?"] * len(ids))
        with self._conn() as conn:
            conn.execute(
                f"UPDATE seen_items SET notified = 1 WHERE item_id IN ({placeholders})",
                ids,
            )
