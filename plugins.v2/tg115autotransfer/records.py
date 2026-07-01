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
STATUS_FAILED_RETRYABLE = "failed_retryable"
STATUS_FAILED_FINAL = "failed_final"
STATUS_SKIPPED_PERMANENT = "skipped_permanent"
STATUS_DEFERRED = "deferred"
STATUS_SKIPPED_DUPLICATE = "skipped_duplicate"
STATUS_IGNORED = "ignored"

# Backward-compatible alias used by old imports/tests.
STATUS_FAILED = STATUS_FAILED_RETRYABLE
STATUS_SKIPPED = STATUS_SKIPPED_PERMANENT

PERMANENT_STATUSES = {
    STATUS_TRANSFERRED,
    STATUS_EXISTING,
    STATUS_IGNORED,
    STATUS_SKIPPED_DUPLICATE,
    STATUS_SKIPPED_PERMANENT,
}

REASON_LOW_QUALITY = "LOW_QUALITY"
REASON_QUALITY_THRESHOLD = "QUALITY_THRESHOLD"
REASON_BDMV_STRUCTURE = "BDMV_STRUCTURE"
REASON_CUSTOM_STRUCTURE = "CUSTOM_STRUCTURE"
REASON_SEASON_MISMATCH = "SEASON_MISMATCH"
REASON_COOLDOWN = "COOLDOWN"
REASON_RUN_LIMIT = "RUN_LIMIT"
REASON_SUBSCRIPTION_LIMIT = "SUBSCRIPTION_LIMIT"
REASON_PROBE_LIMIT = "PROBE_LIMIT"
REASON_NETWORK_ERROR = "NETWORK_ERROR"
REASON_RATE_LIMIT = "RATE_LIMIT"
REASON_DUPLICATE = "DUPLICATE"
REASON_PREVIEW = "PREVIEW"
REASON_MEDIA_EXISTING = "MEDIA_EXISTING"
REASON_NEED_CONFIRM = "NEED_CONFIRM"


def parse_datetime(value: object) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


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
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

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
                    quality_score INTEGER DEFAULT 0,
                    resolution TEXT DEFAULT '',
                    quality_flags TEXT DEFAULT '',
                    structure_flags TEXT DEFAULT '',
                    selected_file_count INTEGER DEFAULT 0,
                    selected_names TEXT DEFAULT '',
                    status TEXT NOT NULL,
                    matched_score INTEGER DEFAULT 0,
                    reason TEXT DEFAULT '',
                    reason_code TEXT DEFAULT '',
                    retryable INTEGER DEFAULT 0,
                    retry_after TEXT DEFAULT '',
                    retry_count INTEGER DEFAULT 0,
                    last_attempt_at TEXT DEFAULT '',
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
                    subscriptions_total INTEGER DEFAULT 0,
                    subscriptions_processed INTEGER DEFAULT 0,
                    subscriptions_remaining INTEGER DEFAULT 0,
                    stopped_early INTEGER DEFAULT 0,
                    stop_reason TEXT DEFAULT '',
                    messages_found INTEGER DEFAULT 0,
                    links_found INTEGER DEFAULT 0,
                    raw_messages_found INTEGER DEFAULT 0,
                    raw_links_found INTEGER DEFAULT 0,
                    unique_messages_found INTEGER DEFAULT 0,
                    unique_links_found INTEGER DEFAULT 0,
                    matched INTEGER DEFAULT 0,
                    previewed INTEGER DEFAULT 0,
                    transferred INTEGER DEFAULT 0,
                    skipped INTEGER DEFAULT 0,
                    skipped_permanent INTEGER DEFAULT 0,
                    deferred INTEGER DEFAULT 0,
                    duplicates INTEGER DEFAULT 0,
                    existing INTEGER DEFAULT 0,
                    need_confirm INTEGER DEFAULT 0,
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
                    trigger_date TEXT DEFAULT '',
                    trigger_count INTEGER DEFAULT 0,
                    retry_after TEXT DEFAULT '',
                    last_result TEXT DEFAULT '',
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
                CREATE INDEX IF NOT EXISTS idx_transfer_records_retry_after ON transfer_records(retry_after);
                CREATE INDEX IF NOT EXISTS idx_search_runs_started ON search_runs(started_at);
                CREATE INDEX IF NOT EXISTS idx_follow_schedules_next ON follow_schedules(enabled, next_run_at);
                CREATE INDEX IF NOT EXISTS idx_follow_schedules_trigger_date ON follow_schedules(trigger_date);
                """
            )
            self._ensure_columns(conn, "transfer_records", {
                "quality_score": "INTEGER DEFAULT 0",
                "resolution": "TEXT DEFAULT ''",
                "quality_flags": "TEXT DEFAULT ''",
                "structure_flags": "TEXT DEFAULT ''",
                "selected_file_count": "INTEGER DEFAULT 0",
                "selected_names": "TEXT DEFAULT ''",
                "reason_code": "TEXT DEFAULT ''",
                "retryable": "INTEGER DEFAULT 0",
                "retry_after": "TEXT DEFAULT ''",
                "last_attempt_at": "TEXT DEFAULT ''",
            })
            self._ensure_columns(conn, "search_runs", {
                "subscriptions_total": "INTEGER DEFAULT 0",
                "subscriptions_processed": "INTEGER DEFAULT 0",
                "subscriptions_remaining": "INTEGER DEFAULT 0",
                "stopped_early": "INTEGER DEFAULT 0",
                "stop_reason": "TEXT DEFAULT ''",
                "raw_messages_found": "INTEGER DEFAULT 0",
                "raw_links_found": "INTEGER DEFAULT 0",
                "unique_messages_found": "INTEGER DEFAULT 0",
                "unique_links_found": "INTEGER DEFAULT 0",
                "skipped_permanent": "INTEGER DEFAULT 0",
                "deferred": "INTEGER DEFAULT 0",
                "duplicates": "INTEGER DEFAULT 0",
                "existing": "INTEGER DEFAULT 0",
                "need_confirm": "INTEGER DEFAULT 0",
            })
            self._ensure_columns(conn, "follow_schedules", {
                "trigger_date": "TEXT DEFAULT ''",
                "trigger_count": "INTEGER DEFAULT 0",
                "retry_after": "TEXT DEFAULT ''",
                "last_result": "TEXT DEFAULT ''",
            })
            self._migrate_legacy_statuses(conn)
            conn.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', '2')")

    @staticmethod
    def _ensure_columns(conn: sqlite3.Connection, table: str, columns: Dict[str, str]) -> None:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def _migrate_legacy_statuses(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute("SELECT id, status, reason, retry_count FROM transfer_records WHERE status IN ('skipped', 'failed')").fetchall()
        for row in rows:
            status = str(row["status"] or "")
            reason = str(row["reason"] or "")
            retry_count = int(row["retry_count"] or 0)
            if status == "skipped":
                lowered = reason.lower()
                if any(key in reason for key in ("冷却", "单轮", "单订阅", "预检", "限流", "稍后")):
                    new_status = STATUS_DEFERRED
                    retryable = 1
                    reason_code = REASON_COOLDOWN if "冷却" in reason else REASON_RUN_LIMIT
                elif any(key in reason for key in ("低质量", "BDMV", "结构", "季不一致", "分辨率")) or any(key in lowered for key in ("bdmv", "quality")):
                    new_status = STATUS_SKIPPED_PERMANENT
                    retryable = 0
                    reason_code = REASON_QUALITY_THRESHOLD
                else:
                    new_status = STATUS_DEFERRED
                    retryable = 1
                    reason_code = REASON_NETWORK_ERROR
            else:
                if retry_count >= 3:
                    new_status = STATUS_FAILED_FINAL
                    retryable = 0
                else:
                    new_status = STATUS_FAILED_RETRYABLE
                    retryable = 1
                reason_code = REASON_NETWORK_ERROR
            conn.execute(
                "UPDATE transfer_records SET status=?, retryable=?, reason_code=?, updated_at=? WHERE id=?",
                (new_status, retryable, reason_code, self.now(), int(row["id"])),
            )

    @staticmethod
    def build_link_key(subscription_id: int, share_code: str, receive_code: str = "") -> str:
        return f"{int(subscription_id)}:{share_code}:{receive_code or ''}"

    def get_record_by_key(self, link_key: str) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM transfer_records WHERE link_key=?", (link_key,)).fetchone()
            return dict(row) if row else None

    def should_skip_record(
        self,
        link_key: str,
        retry_limit: int = 3,
        dry_run: bool = False,
        now: Optional[datetime] = None,
    ) -> tuple[bool, str, Optional[Dict[str, Any]]]:
        record = self.get_record_by_key(link_key)
        if not record:
            return False, "未处理过", None
        current = now or datetime.now()
        status = str(record.get("status") or "")
        if status in PERMANENT_STATUSES:
            return True, f"该链接已有永久处理记录：{status}", record
        if status == STATUS_PREVIEWED:
            if dry_run:
                return True, "该链接已演练预览", record
            return False, "演练记录允许进入真实转存", record
        if status == STATUS_DEFERRED:
            retry_after = parse_datetime(record.get("retry_after"))
            if retry_after and retry_after > current:
                return True, f"尚未到重试时间：{record.get('retry_after')}", record
            return False, "临时延后已到期", record
        if status == STATUS_FAILED_FINAL:
            return True, "失败次数已达到上限", record
        if status == STATUS_FAILED_RETRYABLE:
            if int(record.get("retry_count") or 0) >= int(retry_limit or 3):
                return True, f"该链接失败次数已达到上限：{record.get('retry_count')}", record
            return False, f"允许失败重试：{record.get('retry_count')}", record
        return False, f"允许处理：{status}", record

    def upsert_record(self, **kwargs: Any) -> int:
        now = self.now()
        required = ["subscription_id", "subscription_name", "channel", "share_url", "share_code", "link_key", "status"]
        for key in required:
            if kwargs.get(key) in (None, ""):
                raise ValueError(f"transfer_records 缺少字段：{key}")
        status = str(kwargs.get("status") or "")
        retryable_default = 1 if status in {STATUS_DEFERRED, STATUS_FAILED_RETRYABLE} else 0
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
            "quality_score": int(kwargs.get("quality_score") or 0),
            "resolution": str(kwargs.get("resolution") or ""),
            "quality_flags": str(kwargs.get("quality_flags") or ""),
            "structure_flags": str(kwargs.get("structure_flags") or ""),
            "selected_file_count": int(kwargs.get("selected_file_count") or 0),
            "selected_names": str(kwargs.get("selected_names") or ""),
            "status": status,
            "matched_score": int(kwargs.get("matched_score") or 0),
            "reason": str(kwargs.get("reason") or ""),
            "reason_code": str(kwargs.get("reason_code") or ""),
            "retryable": int(kwargs.get("retryable") if kwargs.get("retryable") is not None else retryable_default),
            "retry_after": str(kwargs.get("retry_after") or ""),
            "retry_count": int(kwargs.get("retry_count") or 0),
            "last_attempt_at": str(kwargs.get("last_attempt_at") or now),
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
                    share_url, share_code, receive_code, link_key, season, episodes, quality,
                    quality_score, resolution, quality_flags, structure_flags, selected_file_count, selected_names, status,
                    matched_score, reason, reason_code, retryable, retry_after, retry_count, last_attempt_at,
                    target_cid, transferred_at, created_at, updated_at
                ) VALUES (
                    :subscription_id, :subscription_name, :channel, :message_id, :message_url, :resource_title,
                    :share_url, :share_code, :receive_code, :link_key, :season, :episodes, :quality,
                    :quality_score, :resolution, :quality_flags, :structure_flags, :selected_file_count, :selected_names, :status,
                    :matched_score, :reason, :reason_code, :retryable, :retry_after, :retry_count, :last_attempt_at,
                    :target_cid, :transferred_at, :created_at, :updated_at
                )
                ON CONFLICT(link_key) DO UPDATE SET
                    channel=excluded.channel,
                    message_id=excluded.message_id,
                    message_url=excluded.message_url,
                    resource_title=excluded.resource_title,
                    season=excluded.season,
                    episodes=excluded.episodes,
                    status=excluded.status,
                    quality=excluded.quality,
                    quality_score=excluded.quality_score,
                    resolution=excluded.resolution,
                    quality_flags=excluded.quality_flags,
                    structure_flags=excluded.structure_flags,
                    selected_file_count=excluded.selected_file_count,
                    selected_names=excluded.selected_names,
                    matched_score=excluded.matched_score,
                    reason=excluded.reason,
                    reason_code=excluded.reason_code,
                    retryable=excluded.retryable,
                    retry_after=excluded.retry_after,
                    retry_count=excluded.retry_count,
                    last_attempt_at=excluded.last_attempt_at,
                    target_cid=excluded.target_cid,
                    transferred_at=excluded.transferred_at,
                    updated_at=excluded.updated_at
                """,
                values,
            )
            row = conn.execute("SELECT id FROM transfer_records WHERE link_key=?", (values["link_key"],)).fetchone()
            return int(row["id"])

    def start_run(self, source: str, subscription_id: int = 0, subscription_name: str = "", keyword: str = "", channels_count: int = 0, subscriptions_total: int = 0) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO search_runs(source, subscription_id, subscription_name, keyword, channels_count, subscriptions_total, started_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (source, int(subscription_id or 0), subscription_name, keyword, int(channels_count or 0), int(subscriptions_total or 0), self.now()),
            )
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, **kwargs: Any) -> None:
        allowed = {
            "messages_found", "links_found", "raw_messages_found", "raw_links_found", "unique_messages_found", "unique_links_found",
            "matched", "previewed", "transferred", "skipped", "skipped_permanent", "deferred", "duplicates", "existing",
            "need_confirm", "failed", "subscriptions_total", "subscriptions_processed", "subscriptions_remaining", "stopped_early",
            "stop_reason", "summary",
        }
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
            "trigger_date": str(schedule.get("trigger_date") or ""),
            "trigger_count": int(schedule.get("trigger_count") or 0),
            "retry_after": str(schedule.get("retry_after") or ""),
            "last_result": str(schedule.get("last_result") or ""),
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
                INSERT INTO follow_schedules(subscribe_id, title, parsed_days, parsed_time, delay_minutes, next_run_at, last_run_at, trigger_date, trigger_count, retry_after, last_result, source, confidence, enabled, raw_text, episode_count, last_web_check_at, created_at, updated_at)
                VALUES (:subscribe_id, :title, :parsed_days, :parsed_time, :delay_minutes, :next_run_at, :last_run_at, :trigger_date, :trigger_count, :retry_after, :last_result, :source, :confidence, :enabled, :raw_text, :episode_count, :last_web_check_at, :created_at, :updated_at)
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

    def due_follow_schedules(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM follow_schedules WHERE enabled=1 AND next_run_at!='' AND next_run_at<=? AND (retry_after='' OR retry_after<=?) ORDER BY next_run_at ASC LIMIT ?",
                (self.now(), self.now(), int(limit)),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_follow_schedules(self) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM follow_schedules ORDER BY next_run_at, title").fetchall()]

    def update_follow_schedule(self, subscribe_id: int, **kwargs: Any) -> None:
        allowed = {
            "enabled", "next_run_at", "last_run_at", "last_web_check_at", "source", "confidence", "raw_text", "episode_count",
            "trigger_date", "trigger_count", "retry_after", "last_result",
        }
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return
        fields["updated_at"] = self.now()
        assignments = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [int(subscribe_id)]
        with self.connect() as conn:
            conn.execute(f"UPDATE follow_schedules SET {assignments} WHERE subscribe_id=?", values)

    def reset_follow_daily_counter_if_needed(self, schedule: Dict[str, Any], today: str) -> Dict[str, Any]:
        item = dict(schedule)
        if str(item.get("trigger_date") or "") != today:
            item["trigger_date"] = today
            item["trigger_count"] = 0
            self.update_follow_schedule(int(item.get("subscribe_id") or 0), trigger_date=today, trigger_count=0)
        return item

    def increment_follow_trigger_count(self, subscribe_id: int, today: str) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT trigger_date, trigger_count FROM follow_schedules WHERE subscribe_id=?", (int(subscribe_id),)).fetchone()
            if not row:
                return 0
            if str(row["trigger_date"] or "") != today:
                count = 1
                conn.execute("UPDATE follow_schedules SET trigger_date=?, trigger_count=?, updated_at=? WHERE subscribe_id=?", (today, count, self.now(), int(subscribe_id)))
                return count
            count = int(row["trigger_count"] or 0) + 1
            conn.execute("UPDATE follow_schedules SET trigger_count=?, updated_at=? WHERE subscribe_id=?", (count, self.now(), int(subscribe_id)))
            return count
