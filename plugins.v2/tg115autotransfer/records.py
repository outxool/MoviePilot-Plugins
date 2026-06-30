from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional


STATUS_PREVIEWED = "previewed"
STATUS_TRANSFERRED = "transferred"
STATUS_EXISTING = "existing"
STATUS_NEED_CONFIRM = "need_confirm"
STATUS_FAILED = "failed"
STATUS_SKIPPED_DUPLICATE = "skipped_duplicate"
STATUS_IGNORED = "ignored"
STATUS_SKIPPED = "skipped"


class Tg115StateStore:
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
                CREATE TABLE IF NOT EXISTS transfer_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id INTEGER NOT NULL,
                    subscription_name TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    message_id INTEGER DEFAULT 0,
                    message_url TEXT DEFAULT '',
                    resource_title TEXT DEFAULT '',
                    share_url TEXT NOT NULL,
                    share_code TEXT NOT NULL,
                    receive_code TEXT DEFAULT '',
                    link_key TEXT NOT NULL UNIQUE,
                    season INTEGER DEFAULT 0,
                    episodes TEXT DEFAULT '',
                    quality TEXT DEFAULT '',
                    status TEXT NOT NULL,
                    matched_score INTEGER DEFAULT 0,
                    reason TEXT DEFAULT '',
                    retry_count INTEGER DEFAULT 0,
                    target_cid TEXT DEFAULT '',
                    transferred_at TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS search_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    subscription_id INTEGER DEFAULT 0,
                    subscription_name TEXT DEFAULT '',
                    keyword TEXT DEFAULT '',
                    channels_count INTEGER DEFAULT 0,
                    messages_found INTEGER DEFAULT 0,
                    links_found INTEGER DEFAULT 0,
                    matched INTEGER DEFAULT 0,
                    previewed INTEGER DEFAULT 0,
                    transferred INTEGER DEFAULT 0,
                    skipped INTEGER DEFAULT 0,
                    failed INTEGER DEFAULT 0,
                    started_at TEXT NOT NULL,
                    finished_at TEXT DEFAULT '',
                    summary TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS follow_schedules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscribe_id INTEGER NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    parsed_days TEXT DEFAULT '',
                    parsed_time TEXT DEFAULT '',
                    delay_minutes INTEGER DEFAULT 35,
                    next_run_at TEXT DEFAULT '',
                    last_run_at TEXT DEFAULT '',
                    source TEXT DEFAULT '',
                    confidence TEXT DEFAULT '',
                    enabled INTEGER DEFAULT 1,
                    raw_text TEXT DEFAULT '',
                    episode_count INTEGER DEFAULT 0,
                    last_web_check_at TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_transfer_records_sub ON transfer_records(subscription_id);
                CREATE INDEX IF NOT EXISTS idx_transfer_records_status ON transfer_records(status);
                CREATE INDEX IF NOT EXISTS idx_transfer_records_share_code ON transfer_records(share_code);
                CREATE INDEX IF NOT EXISTS idx_search_runs_started ON search_runs(started_at);
                CREATE INDEX IF NOT EXISTS idx_follow_schedules_next ON follow_schedules(enabled, next_run_at);
                """
            )

    @staticmethod
    def build_link_key(subscription_id: int, share_code: str, receive_code: str = "") -> str:
        return f"{int(subscription_id)}:{share_code}:{receive_code or ''}"

    def get_record_by_key(self, link_key: str) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM transfer_records WHERE link_key=?", (link_key,)).fetchone()
            return dict(row) if row else None

    def should_skip_record(self, link_key: str, retry_limit: int = 3) -> tuple[bool, str, Optional[Dict[str, Any]]]:
        record = self.get_record_by_key(link_key)
        if not record:
            return False, "未处理过", None
        status = str(record.get("status") or "")
        if status in {STATUS_TRANSFERRED, STATUS_EXISTING, STATUS_IGNORED, STATUS_SKIPPED_DUPLICATE, STATUS_PREVIEWED}:
            return True, f"该链接已有处理记录：{status}", record
        if status == STATUS_FAILED and int(record.get("retry_count") or 0) >= int(retry_limit or 3):
            return True, f"该链接失败次数已达到上限：{record.get('retry_count')}", record
        return False, f"允许重试：{status}", record

    def upsert_record(self, **kwargs: Any) -> int:
        now = self.now()
        required = ["subscription_id", "subscription_name", "channel", "share_url", "share_code", "link_key", "status"]
        for key in required:
            if kwargs.get(key) in (None, ""):
                raise ValueError(f"transfer_records 缺少字段：{key}")
        values = {
            "subscription_id": int(kwargs.get("subscription_id") or 0),
            "subscription_name": str(kwargs.get("subscription_name") or ""),
            "channel": str(kwargs.get("channel") or ""),
            "message_id": int(kwargs.get("message_id") or 0),
            "message_url": str(kwargs.get("message_url") or ""),
            "resource_title": str(kwargs.get("resource_title") or ""),
            "share_url": str(kwargs.get("share_url") or ""),
            "share_code": str(kwargs.get("share_code") or ""),
            "receive_code": str(kwargs.get("receive_code") or ""),
            "link_key": str(kwargs.get("link_key") or ""),
            "season": int(kwargs.get("season") or 0),
            "episodes": str(kwargs.get("episodes") or ""),
            "quality": str(kwargs.get("quality") or ""),
            "status": str(kwargs.get("status") or ""),
            "matched_score": int(kwargs.get("matched_score") or 0),
            "reason": str(kwargs.get("reason") or ""),
            "retry_count": int(kwargs.get("retry_count") or 0),
            "target_cid": str(kwargs.get("target_cid") or ""),
            "transferred_at": str(kwargs.get("transferred_at") or ""),
            "created_at": now,
            "updated_at": now,
        }
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO transfer_records(
                    subscription_id, subscription_name, channel, message_id, message_url, resource_title,
                    share_url, share_code, receive_code, link_key, season, episodes, quality, status,
                    matched_score, reason, retry_count, target_cid, transferred_at, created_at, updated_at
                ) VALUES (
                    :subscription_id, :subscription_name, :channel, :message_id, :message_url, :resource_title,
                    :share_url, :share_code, :receive_code, :link_key, :season, :episodes, :quality, :status,
                    :matched_score, :reason, :retry_count, :target_cid, :transferred_at, :created_at, :updated_at
                )
                ON CONFLICT(link_key) DO UPDATE SET
                    status=excluded.status,
                    matched_score=excluded.matched_score,
                    reason=excluded.reason,
                    retry_count=excluded.retry_count,
                    target_cid=excluded.target_cid,
                    transferred_at=excluded.transferred_at,
                    updated_at=excluded.updated_at
                """,
                values,
            )
            row = conn.execute("SELECT id FROM transfer_records WHERE link_key=?", (values["link_key"],)).fetchone()
            return int(row["id"])

    def increment_failed(self, link_key: str, reason: str) -> None:
        now = self.now()
        with self.connect() as conn:
            row = conn.execute("SELECT retry_count FROM transfer_records WHERE link_key=?", (link_key,)).fetchone()
            retry = int(row["retry_count"] if row else 0) + 1
            conn.execute(
                "UPDATE transfer_records SET status=?, retry_count=?, reason=?, updated_at=? WHERE link_key=?",
                (STATUS_FAILED, retry, reason[:1000], now, link_key),
            )

    def start_run(self, source: str, subscription_id: int = 0, subscription_name: str = "", keyword: str = "", channels_count: int = 0) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO search_runs(source, subscription_id, subscription_name, keyword, channels_count, started_at) VALUES (?, ?, ?, ?, ?, ?)",
                (source, int(subscription_id or 0), subscription_name, keyword, int(channels_count or 0), self.now()),
            )
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, **kwargs: Any) -> None:
        allowed = {"messages_found", "links_found", "matched", "previewed", "transferred", "skipped", "failed", "summary"}
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        fields["finished_at"] = self.now()
        assignments = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [int(run_id)]
        with self.connect() as conn:
            conn.execute(f"UPDATE search_runs SET {assignments} WHERE id=?", values)

    def stats(self) -> Dict[str, Any]:
        with self.connect() as conn:
            by_status = {row["status"]: row["c"] for row in conn.execute("SELECT status, COUNT(*) AS c FROM transfer_records GROUP BY status").fetchall()}
            total_records = conn.execute("SELECT COUNT(*) AS c FROM transfer_records").fetchone()["c"]
            today = datetime.now().strftime("%Y-%m-%d") + "%"
            today_runs = conn.execute("SELECT COUNT(*) AS c FROM search_runs WHERE started_at LIKE ?", (today,)).fetchone()["c"]
            follow_count = conn.execute("SELECT COUNT(*) AS c FROM follow_schedules WHERE enabled=1").fetchone()["c"]
            next_follow = conn.execute("SELECT title, next_run_at FROM follow_schedules WHERE enabled=1 AND next_run_at!='' ORDER BY next_run_at ASC LIMIT 1").fetchone()
            last_runs = [dict(row) for row in conn.execute("SELECT * FROM search_runs ORDER BY id DESC LIMIT 5").fetchall()]
            recent_records = [dict(row) for row in conn.execute("SELECT * FROM transfer_records ORDER BY id DESC LIMIT 10").fetchall()]
        return {
            "records": int(total_records),
            "by_status": by_status,
            "today_runs": int(today_runs),
            "follow_count": int(follow_count),
            "next_follow": dict(next_follow) if next_follow else None,
            "last_runs": last_runs,
            "recent_records": recent_records,
        }

    def clear_records(self) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM transfer_records")
            conn.execute("DELETE FROM search_runs")

    def upsert_follow_schedule(self, schedule: Dict[str, Any]) -> int:
        now = self.now()
        values = {
            "subscribe_id": int(schedule.get("subscribe_id") or 0),
            "title": str(schedule.get("title") or ""),
            "parsed_days": str(schedule.get("parsed_days") or ""),
            "parsed_time": str(schedule.get("parsed_time") or ""),
            "delay_minutes": int(schedule.get("delay_minutes") or 35),
            "next_run_at": str(schedule.get("next_run_at") or ""),
            "last_run_at": str(schedule.get("last_run_at") or ""),
            "source": str(schedule.get("source") or ""),
            "confidence": str(schedule.get("confidence") or ""),
            "enabled": 1 if bool(schedule.get("enabled", True)) else 0,
            "raw_text": str(schedule.get("raw_text") or ""),
            "episode_count": int(schedule.get("episode_count") or 0),
            "last_web_check_at": str(schedule.get("last_web_check_at") or ""),
            "created_at": now,
            "updated_at": now,
        }
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO follow_schedules(subscribe_id, title, parsed_days, parsed_time, delay_minutes, next_run_at, last_run_at, source, confidence, enabled, raw_text, episode_count, last_web_check_at, created_at, updated_at)
                VALUES (:subscribe_id, :title, :parsed_days, :parsed_time, :delay_minutes, :next_run_at, :last_run_at, :source, :confidence, :enabled, :raw_text, :episode_count, :last_web_check_at, :created_at, :updated_at)
                ON CONFLICT(subscribe_id) DO UPDATE SET
                    title=excluded.title,
                    parsed_days=excluded.parsed_days,
                    parsed_time=excluded.parsed_time,
                    delay_minutes=excluded.delay_minutes,
                    next_run_at=excluded.next_run_at,
                    source=excluded.source,
                    confidence=excluded.confidence,
                    enabled=excluded.enabled,
                    raw_text=excluded.raw_text,
                    episode_count=excluded.episode_count,
                    last_web_check_at=CASE WHEN excluded.last_web_check_at!='' THEN excluded.last_web_check_at ELSE follow_schedules.last_web_check_at END,
                    updated_at=excluded.updated_at
                """,
                values,
            )
            row = conn.execute("SELECT id FROM follow_schedules WHERE subscribe_id=?", (values["subscribe_id"],)).fetchone()
            return int(row["id"])

    def due_follow_schedules(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM follow_schedules WHERE enabled=1 AND next_run_at!='' AND next_run_at<=? ORDER BY next_run_at ASC LIMIT ?",
                (self.now(), int(limit)),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_follow_schedules(self) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM follow_schedules ORDER BY next_run_at, title").fetchall()]

    def update_follow_schedule(self, subscribe_id: int, **kwargs: Any) -> None:
        allowed = {"enabled", "next_run_at", "last_run_at", "last_web_check_at", "source", "confidence", "raw_text", "episode_count"}
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return
        fields["updated_at"] = self.now()
        assignments = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [int(subscribe_id)]
        with self.connect() as conn:
            conn.execute(f"UPDATE follow_schedules SET {assignments} WHERE subscribe_id=?", values)
