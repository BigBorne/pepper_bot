# -*- coding: utf-8 -*-
"""SQLite-хранилище очереди публикаций, промокодов и статистики."""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from functools import wraps
from typing import Iterable

log = logging.getLogger("pepper.store")
MAX_ATTEMPTS = 5


def _synchronized(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class Store:
    def __init__(self, path: str) -> None:
        self._lock = threading.RLock()
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS posted (
                deal_id INTEGER PRIMARY KEY,
                title TEXT,
                posted_at TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT
            )
        """)
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS promocodes (
                deal_id INTEGER PRIMARY KEY,
                code TEXT NOT NULL,
                title TEXT,
                price REAL,
                old_price REAL,
                merchant TEXT,
                url TEXT,
                product_url TEXT,
                seen_at REAL NOT NULL
            )
        """)
        self.con.execute("CREATE INDEX IF NOT EXISTS idx_promocodes_seen_at ON promocodes(seen_at)")
        self.con.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        # Миграция базы, созданной предыдущей версией.
        columns = {r[1] for r in self.con.execute("PRAGMA table_info(posted)")}
        for name, definition in (
            ("status", "TEXT NOT NULL DEFAULT 'published'"),
            ("collected_at", "REAL"),
            ("published_at", "REAL"),
            ("skipped_at", "REAL"),
            ("payload", "TEXT"),
        ):
            if name not in columns:
                self.con.execute(f"ALTER TABLE posted ADD COLUMN {name} {definition}")
        self.con.execute("UPDATE posted SET status='seeded', last_error=NULL WHERE last_error='seeded'")
        # Старые записи до введения collected_at: восстановить время из posted_at.
        self.con.execute(
            "UPDATE posted SET collected_at=strftime('%s', posted_at) "
            "WHERE collected_at IS NULL AND posted_at IS NOT NULL AND posted_at != ''"
        )
        self.con.execute(
            "UPDATE posted SET published_at=collected_at "
            "WHERE status='published' AND published_at IS NULL AND collected_at IS NOT NULL"
        )
        promo_columns = {r[1] for r in self.con.execute("PRAGMA table_info(promocodes)")}
        if "product_url" not in promo_columns:
            self.con.execute("ALTER TABLE promocodes ADD COLUMN product_url TEXT")
        self.con.commit()

    @_synchronized
    def is_empty(self) -> bool:
        return not self.con.execute("SELECT EXISTS(SELECT 1 FROM posted)").fetchone()[0]

    @_synchronized
    def seed(self, deal_ids: Iterable[int], titles: dict[int, str] | None = None) -> int:
        titles = titles or {}
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows = [(int(i), titles.get(int(i), ""), now, 0, "seeded") for i in deal_ids]
        self.con.executemany(
            "INSERT OR IGNORE INTO posted(deal_id,title,posted_at,attempts,last_error,status) "
            "VALUES (?, ?, ?, ?, ?, 'seeded')", rows,
        )
        self.con.commit()
        log.info("seeded %d deals", len(rows))
        return len(rows)

    @_synchronized
    def should_process(self, deal_id: int) -> bool:
        row = self.con.execute(
            "SELECT status, attempts FROM posted WHERE deal_id = ?", (int(deal_id),)
        ).fetchone()
        if row is None:
            return True
        return row[0] == "failed" and row[1] < MAX_ATTEMPTS

    @_synchronized
    def known_ids(self) -> set[int]:
        return {row[0] for row in self.con.execute("SELECT deal_id FROM posted")}

    def mark_attempt(self, deal_id: int, title: str, error: str) -> None:
        self.mark_failed(deal_id, title, error)

    @_synchronized
    def save_deal(self, payload: dict, status: str = "pending") -> None:
        """Сохраняет полный Deal перед публикацией/модерацией."""
        deal_id = int(payload["deal_id"])
        title = payload.get("title", "")
        now = time.time()
        existing = self.con.execute("SELECT status FROM posted WHERE deal_id=?", (deal_id,)).fetchone()
        if existing and existing[0] in ("published", "skipped", "pending"):
            return
        self.con.execute(
            "INSERT INTO posted(deal_id,title,posted_at,attempts,last_error,status,collected_at,payload) "
            "VALUES (?, ?, NULL, 0, NULL, ?, ?, ?) "
            "ON CONFLICT(deal_id) DO UPDATE SET title=excluded.title, status=excluded.status, "
            "collected_at=COALESCE(posted.collected_at, excluded.collected_at), payload=excluded.payload",
            (deal_id, title, status, now, json.dumps(payload, ensure_ascii=False)),
        )
        self.con.commit()

    @_synchronized
    def get_record(self, deal_id: int) -> dict | None:
        row = self.con.execute(
            "SELECT deal_id,title,status,attempts,last_error,payload FROM posted WHERE deal_id = ?",
            (int(deal_id),),
        ).fetchone()
        if not row:
            return None
        data = json.loads(row[5]) if row[5] else {"deal_id": row[0], "title": row[1]}
        data.update({"deal_id": row[0], "title": row[1], "status": row[2],
                     "attempts": row[3], "last_error": row[4]})
        data["payload"] = json.loads(row[5]) if row[5] else {}
        return data

    @_synchronized
    def pending(self) -> list[dict]:
        rows = self.con.execute(
            "SELECT deal_id FROM posted WHERE status = 'pending' ORDER BY collected_at"
        ).fetchall()
        return [self.get_record(row[0]) for row in rows]

    @_synchronized
    def update_pending(self, payload: dict) -> None:
        self.con.execute(
            "UPDATE posted SET title=?, payload=? WHERE deal_id=? AND status='pending'",
            (payload.get("title", ""), json.dumps(payload, ensure_ascii=False), int(payload["deal_id"])),
        )
        self.con.commit()

    @_synchronized
    def get_setting(self, key: str, default: str = "") -> str:
        row = self.con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    @_synchronized
    def set_setting(self, key: str, value: str) -> None:
        self.con.execute(
            "INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.con.commit()

    @_synchronized
    def mark_posted(self, deal_id: int, title: str = "") -> None:
        now = time.time()
        self.con.execute(
            "UPDATE posted SET title=COALESCE(NULLIF(?, ''), title), status='published', "
            "published_at=?, posted_at=? WHERE deal_id=?",
            (title, now, datetime.now(timezone.utc).isoformat(timespec="seconds"), int(deal_id)),
        )
        self.con.commit()

    @_synchronized
    def mark_skipped(self, deal_id: int) -> None:
        self.con.execute(
            "UPDATE posted SET status='skipped', skipped_at=? WHERE deal_id=?",
            (time.time(), int(deal_id)),
        )
        self.con.commit()

    @_synchronized
    def mark_failed(self, deal_id: int, title: str, error: str) -> None:
        self.con.execute(
            "INSERT INTO posted(deal_id,title,status,attempts,last_error,collected_at) VALUES (?, ?, 'failed', 1, ?, ?) "
            "ON CONFLICT(deal_id) DO UPDATE SET title=excluded.title, status='failed', "
            "attempts=posted.attempts+1, last_error=excluded.last_error, "
            "collected_at=COALESCE(posted.collected_at, excluded.collected_at)",
            (int(deal_id), title, error[:1000], time.time()),
        )
        self.con.commit()

    @_synchronized
    def save_promo(self, deal_id: int, code: str, title: str, price=None,
                   old_price=None, merchant=None, url: str = "",
                   product_url: str | None = None) -> None:
        self.con.execute(
            "INSERT INTO promocodes(deal_id,code,title,price,old_price,merchant,url,product_url,seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(deal_id) DO UPDATE SET code=excluded.code,title=excluded.title,price=excluded.price, "
            "old_price=excluded.old_price,merchant=excluded.merchant,url=excluded.url, "
            "product_url=excluded.product_url,seen_at=excluded.seen_at",
            (int(deal_id), code, title, price, old_price, merchant, url, product_url, time.time()),
        )
        self.con.commit()

    @_synchronized
    def promos_since(self, hours: float) -> list[dict]:
        cur = self.con.execute(
            "SELECT deal_id,code,title,price,old_price,merchant,url,product_url,seen_at "
            "FROM promocodes WHERE seen_at >= ? ORDER BY seen_at DESC",
            (time.time() - hours * 3600,),
        )
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]

    @_synchronized
    def stats(self, hours: float) -> dict:
        since = time.time() - hours * 3600
        total = self.con.execute("SELECT COUNT(*) FROM posted WHERE collected_at >= ?", (since,)).fetchone()[0]
        rows = self.con.execute(
            "SELECT status, COUNT(*) FROM posted WHERE collected_at >= ? GROUP BY status", (since,)
        ).fetchall()
        result = {"period_hours": hours, "collected": total, "published": 0,
                  "skipped": 0, "pending": 0, "failed": 0, "seeded": 0}
        result.update({status: count for status, count in rows})
        result["errors"] = self.con.execute(
            "SELECT COUNT(*) FROM posted WHERE collected_at >= ? AND last_error IS NOT NULL", (since,)
        ).fetchone()[0]
        return result

    @_synchronized
    def recent_errors(self, limit: int = 10) -> list[dict]:
        cur = self.con.execute(
            "SELECT deal_id,title,last_error,attempts FROM posted "
            "WHERE last_error IS NOT NULL ORDER BY COALESCE(collected_at,0) DESC LIMIT ?",
            (int(limit),),
        )
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]

    @_synchronized
    def close(self) -> None:
        self.con.close()
