from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

from .models import ShareLink, TelegramResource


STATUS_PENDING = "pending"
STATUS_MATCHED = "matched"
STATUS_TRANSFERRED = "transferred"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"
STATUS_IGNORED = "ignored"
STATUS_NEED_CONFIRM = "need_confirm"
STATUS_EXISTING = "existing"


class Tg115HistoryStore:
    """SQLite-backed TG resource history store for TG115AutoTransfer."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    @staticmethod
    def now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS resources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    text TEXT NOT NULL,
                    published_at TEXT DEFAULT '',
                    message_url TEXT DEFAULT '',
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(channel, message_id)
                );

                CREATE TABLE IF NOT EXISTS links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    resource_id INTEGER NOT NULL,
                    channel TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    url TEXT NOT NULL,
                    share_code TEXT NOT NULL,
                    receive_code TEXT DEFAULT '',
                    link_key TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    matched_subscription_id INTEGER DEFAULT 0,
                    matched_subscription_name TEXT DEFAULT '',
                    matched_score INTEGER DEFAULT 0,
                    matched_at TEXT DEFAULT '',
                    transferred_at TEXT DEFAULT '',
                    target_cid TEXT DEFAULT '',
                    error_message TEXT DEFAULT '',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(link_key),
                    FOREIGN KEY(resource_id) REFERENCES resources(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS channel_state (
                    channel TEXT PRIMARY KEY,
                    newest_id INTEGER DEFAULT 0,
                    oldest_id INTEGER DEFAULT 0,
                    backfill_before_id INTEGER DEFAULT 0,
                    backfill_complete INTEGER DEFAULT 0,
                    last_increment_scan_at TEXT DEFAULT '',
                    last_backfill_at TEXT DEFAULT '',
                    total_pages INTEGER DEFAULT 0,
                    total_resources INTEGER DEFAULT 0,
                    last_error TEXT DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT DEFAULT '',
                    channels INTEGER DEFAULT 0,
                    backfill_pages INTEGER DEFAULT 0,
                    resources_added INTEGER DEFAULT 0,
                    links_added INTEGER DEFAULT 0,
                    matched INTEGER DEFAULT 0,
                    transferred INTEGER DEFAULT 0,
                    skipped INTEGER DEFAULT 0,
                    failed INTEGER DEFAULT 0,
                    summary TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS follow_schedules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscribe_id INTEGER NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    tmdbid TEXT DEFAULT '',
                    season INTEGER DEFAULT 0,
                    enabled INTEGER DEFAULT 1,
                    raw_text TEXT DEFAULT '',
                    parsed_days TEXT DEFAULT '',
                    parsed_time TEXT DEFAULT '',
                    episode_count INTEGER DEFAULT 0,
                    delay_minutes INTEGER DEFAULT 35,
                    source TEXT DEFAULT '',
                    confidence TEXT DEFAULT '',
                    next_run_at TEXT DEFAULT '',
                    last_run_at TEXT DEFAULT '',
                    last_web_check_at TEXT DEFAULT '',
                    last_result TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_follow_schedules_next_run ON follow_schedules(enabled, next_run_at);
                CREATE INDEX IF NOT EXISTS idx_follow_schedules_title ON follow_schedules(title);

                CREATE INDEX IF NOT EXISTS idx_resources_channel_message ON resources(channel, message_id);
                CREATE INDEX IF NOT EXISTS idx_links_status ON links(status);
                CREATE INDEX IF NOT EXISTS idx_links_share_code ON links(share_code);
                CREATE INDEX IF NOT EXISTS idx_links_matched_sub ON links(matched_subscription_id);
                CREATE INDEX IF NOT EXISTS idx_links_channel_message ON links(channel, message_id);
                """
            )

    @staticmethod
    def link_key(share: ShareLink, content_hash: str) -> str:
        return f"{share.share_code}:{share.receive_code}:{content_hash}"

    def upsert_resources(self, resources: Iterable[TelegramResource]) -> Dict[str, int]:
        added_resources = 0
        updated_resources = 0
        added_links = 0
        now = self.now()
        with self.connect() as conn:
            for resource in resources:
                existing = conn.execute(
                    "SELECT id, content_hash FROM resources WHERE channel=? AND message_id=?",
                    (resource.channel, resource.message_id),
                ).fetchone()
                if existing:
                    resource_id = int(existing["id"])
                    if existing["content_hash"] != resource.content_hash:
                        conn.execute(
                            """
                            UPDATE resources
                            SET title=?, text=?, published_at=?, message_url=?, content_hash=?, updated_at=?
                            WHERE id=?
                            """,
                            (
                                resource.title,
                                resource.text,
                                resource.published_at,
                                resource.message_url,
                                resource.content_hash,
                                now,
                                resource_id,
                            ),
                        )
                        updated_resources += 1
                else:
                    cur = conn.execute(
                        """
                        INSERT INTO resources(channel, message_id, title, text, published_at, message_url, content_hash, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            resource.channel,
                            resource.message_id,
                            resource.title,
                            resource.text,
                            resource.published_at,
                            resource.message_url,
                            resource.content_hash,
                            now,
                            now,
                        ),
                    )
                    resource_id = int(cur.lastrowid)
                    added_resources += 1

                for share in resource.links:
                    key = self.link_key(share, resource.content_hash)
                    cur = conn.execute(
                        """
                        INSERT OR IGNORE INTO links(
                            resource_id, channel, message_id, url, share_code, receive_code, link_key,
                            status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            resource_id,
                            resource.channel,
                            resource.message_id,
                            share.url,
                            share.share_code,
                            share.receive_code,
                            key,
                            STATUS_PENDING,
                            now,
                            now,
                        ),
                    )
                    if cur.rowcount:
                        added_links += 1
        return {"resources_added": added_resources, "resources_updated": updated_resources, "links_added": added_links}

    def get_channel_state(self, channel: str) -> Dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM channel_state WHERE channel=?", (channel,)).fetchone()
            if not row:
                now = self.now()
                conn.execute(
                    "INSERT INTO channel_state(channel, updated_at) VALUES (?, ?)",
                    (channel, now),
                )
                return {
                    "channel": channel,
                    "newest_id": 0,
                    "oldest_id": 0,
                    "backfill_before_id": 0,
                    "backfill_complete": 0,
                    "last_increment_scan_at": "",
                    "last_backfill_at": "",
                    "total_pages": 0,
                    "total_resources": 0,
                    "last_error": "",
                    "updated_at": now,
                }
            return dict(row)

    def update_channel_state(self, channel: str, **kwargs: Any) -> None:
        allowed = {
            "newest_id",
            "oldest_id",
            "backfill_before_id",
            "backfill_complete",
            "last_increment_scan_at",
            "last_backfill_at",
            "total_pages",
            "total_resources",
            "last_error",
        }
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        fields["updated_at"] = self.now()
        self.get_channel_state(channel)
        assignments = ", ".join(f"{key}=?" for key in fields)
        values = list(fields.values()) + [channel]
        with self.connect() as conn:
            conn.execute(f"UPDATE channel_state SET {assignments} WHERE channel=?", values)

    def pending_links(self, limit: int = 500, retry_limit: int = 3) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    l.*,
                    r.title AS resource_title,
                    r.text AS resource_text,
                    r.published_at AS resource_published_at,
                    r.message_url AS resource_message_url,
                    r.content_hash AS resource_content_hash
                FROM links l
                JOIN resources r ON r.id = l.resource_id
                WHERE l.status IN (?, ?, ?)
                  AND l.retry_count < ?
                ORDER BY l.id ASC
                LIMIT ?
                """,
                (STATUS_PENDING, STATUS_FAILED, STATUS_MATCHED, retry_limit, int(limit)),
            ).fetchall()
            return [dict(row) for row in rows]

    def update_link_status(self, link_id: int, status: str, **kwargs: Any) -> None:
        allowed = {
            "matched_subscription_id",
            "matched_subscription_name",
            "matched_score",
            "matched_at",
            "transferred_at",
            "target_cid",
            "error_message",
            "retry_count",
        }
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        fields["status"] = status
        fields["updated_at"] = self.now()
        assignments = ", ".join(f"{key}=?" for key in fields)
        values = list(fields.values()) + [int(link_id)]
        with self.connect() as conn:
            conn.execute(f"UPDATE links SET {assignments} WHERE id=?", values)

    def increment_retry(self, link_id: int, error_message: str, status: str = STATUS_FAILED) -> None:
        with self.connect() as conn:
            row = conn.execute("SELECT retry_count FROM links WHERE id=?", (int(link_id),)).fetchone()
            retry_count = int(row["retry_count"] if row else 0) + 1
            conn.execute(
                "UPDATE links SET status=?, retry_count=?, error_message=?, updated_at=? WHERE id=?",
                (status, retry_count, error_message[:1000], self.now(), int(link_id)),
            )

    def stats(self) -> Dict[str, Any]:
        with self.connect() as conn:
            resource_count = conn.execute("SELECT COUNT(*) AS c FROM resources").fetchone()["c"]
            link_count = conn.execute("SELECT COUNT(*) AS c FROM links").fetchone()["c"]
            by_status = {
                row["status"]: row["c"]
                for row in conn.execute("SELECT status, COUNT(*) AS c FROM links GROUP BY status").fetchall()
            }
            channels = [dict(row) for row in conn.execute("SELECT * FROM channel_state ORDER BY channel").fetchall()]
            last_runs = [dict(row) for row in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 5").fetchall()]
        return {
            "resources": int(resource_count),
            "links": int(link_count),
            "by_status": by_status,
            "channels": channels,
            "last_runs": last_runs,
        }

    def start_run(self, source: str, channels: int = 0) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO runs(source, started_at, channels) VALUES (?, ?, ?)",
                (source, self.now(), int(channels)),
            )
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, **kwargs: Any) -> None:
        allowed = {
            "backfill_pages",
            "resources_added",
            "links_added",
            "matched",
            "transferred",
            "skipped",
            "failed",
            "summary",
        }
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        fields["finished_at"] = self.now()
        assignments = ", ".join(f"{key}=?" for key in fields)
        values = list(fields.values()) + [int(run_id)]
        with self.connect() as conn:
            conn.execute(f"UPDATE runs SET {assignments} WHERE id=?", values)

    def reset_backfill(self, channels: Iterable[str]) -> None:
        now = self.now()
        with self.connect() as conn:
            for channel in channels:
                conn.execute(
                    """
                    INSERT INTO channel_state(channel, backfill_before_id, backfill_complete, last_error, updated_at)
                    VALUES (?, 0, 0, '', ?)
                    ON CONFLICT(channel) DO UPDATE SET
                        backfill_before_id=0,
                        backfill_complete=0,
                        last_error='',
                        updated_at=excluded.updated_at
                    """,
                    (channel, now),
                )

    def clear_all(self) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM links")
            conn.execute("DELETE FROM resources")
            conn.execute("DELETE FROM channel_state")
            conn.execute("DELETE FROM runs")

    def upsert_follow_schedule(self, schedule: Dict[str, Any]) -> int:
        now = self.now()
        subscribe_id = int(schedule.get("subscribe_id") or 0)
        title = str(schedule.get("title") or "").strip()
        if subscribe_id <= 0 or not title:
            raise ValueError("subscribe_id 和 title 不能为空")
        values = {
            "subscribe_id": subscribe_id,
            "title": title,
            "tmdbid": str(schedule.get("tmdbid") or ""),
            "season": int(schedule.get("season") or 0),
            "enabled": 1 if bool(schedule.get("enabled", True)) else 0,
            "raw_text": str(schedule.get("raw_text") or ""),
            "parsed_days": str(schedule.get("parsed_days") or ""),
            "parsed_time": str(schedule.get("parsed_time") or ""),
            "episode_count": int(schedule.get("episode_count") or 0),
            "delay_minutes": int(schedule.get("delay_minutes") or 35),
            "source": str(schedule.get("source") or ""),
            "confidence": str(schedule.get("confidence") or ""),
            "next_run_at": str(schedule.get("next_run_at") or ""),
            "last_web_check_at": str(schedule.get("last_web_check_at") or ""),
            "last_result": str(schedule.get("last_result") or ""),
            "created_at": now,
            "updated_at": now,
        }
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO follow_schedules(
                    subscribe_id, title, tmdbid, season, enabled, raw_text, parsed_days, parsed_time,
                    episode_count, delay_minutes, source, confidence, next_run_at, last_web_check_at,
                    last_result, created_at, updated_at
                ) VALUES (
                    :subscribe_id, :title, :tmdbid, :season, :enabled, :raw_text, :parsed_days, :parsed_time,
                    :episode_count, :delay_minutes, :source, :confidence, :next_run_at, :last_web_check_at,
                    :last_result, :created_at, :updated_at
                )
                ON CONFLICT(subscribe_id) DO UPDATE SET
                    title=excluded.title,
                    tmdbid=excluded.tmdbid,
                    season=excluded.season,
                    enabled=excluded.enabled,
                    raw_text=excluded.raw_text,
                    parsed_days=excluded.parsed_days,
                    parsed_time=excluded.parsed_time,
                    episode_count=excluded.episode_count,
                    delay_minutes=excluded.delay_minutes,
                    source=excluded.source,
                    confidence=excluded.confidence,
                    next_run_at=excluded.next_run_at,
                    last_web_check_at=CASE WHEN excluded.last_web_check_at != '' THEN excluded.last_web_check_at ELSE follow_schedules.last_web_check_at END,
                    last_result=CASE WHEN excluded.last_result != '' THEN excluded.last_result ELSE follow_schedules.last_result END,
                    updated_at=excluded.updated_at
                """,
                values,
            )
            row = conn.execute("SELECT id FROM follow_schedules WHERE subscribe_id=?", (subscribe_id,)).fetchone()
            return int(row["id"] if row else cur.lastrowid)

    def list_follow_schedules(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            if enabled_only:
                rows = conn.execute("SELECT * FROM follow_schedules WHERE enabled=1 ORDER BY next_run_at, title").fetchall()
            else:
                rows = conn.execute("SELECT * FROM follow_schedules ORDER BY title").fetchall()
            return [dict(row) for row in rows]

    def due_follow_schedules(self, now_text: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        now_text = now_text or self.now()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM follow_schedules
                WHERE enabled=1 AND parsed_time != '' AND next_run_at != '' AND next_run_at <= ?
                ORDER BY next_run_at ASC
                LIMIT ?
                """,
                (now_text, int(limit)),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_follow_schedule(self, subscribe_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM follow_schedules WHERE subscribe_id=?", (int(subscribe_id),)).fetchone()
            return dict(row) if row else None

    def update_follow_schedule(self, subscribe_id: int, **kwargs: Any) -> None:
        allowed = {
            "enabled",
            "raw_text",
            "parsed_days",
            "parsed_time",
            "episode_count",
            "delay_minutes",
            "source",
            "confidence",
            "next_run_at",
            "last_run_at",
            "last_web_check_at",
            "last_result",
        }
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return
        fields["updated_at"] = self.now()
        assignments = ", ".join(f"{key}=?" for key in fields)
        values = list(fields.values()) + [int(subscribe_id)]
        with self.connect() as conn:
            conn.execute(f"UPDATE follow_schedules SET {assignments} WHERE subscribe_id=?", values)
