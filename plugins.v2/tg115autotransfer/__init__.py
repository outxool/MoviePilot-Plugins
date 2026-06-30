from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger

from app.core.event import eventmanager
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType

from .matcher import SubscriptionMatcher
from .models import ShareLink, SubscriptionInfo, TelegramResource
from .p115 import P115TransferClient
from .store import (
    STATUS_EXISTING,
    STATUS_FAILED,
    STATUS_IGNORED,
    STATUS_MATCHED,
    STATUS_NEED_CONFIRM,
    STATUS_PENDING,
    STATUS_SKIPPED,
    STATUS_TRANSFERRED,
    Tg115HistoryStore,
)
from .telegram import TelegramPublicClient
from .text import (
    cloud_path_to_pan_path,
    extract_quality,
    looks_like_low_quality,
    normalize_posix_path,
    parse_episodes,
    parse_season,
)


class Tg115AutoTransfer(_PluginBase):
    plugin_name = "TG 115自动转存"
    plugin_desc = "先保存TG里的115资源，再按订阅缺什么、媒体库有没有，慢慢安全转存"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/cloud.png"
    plugin_version = "0.4.1"
    plugin_author = "outxool"
    author_url = "https://github.com/outxool"
    plugin_config_prefix = "tg115autotransfer_"
    plugin_order = 2
    auth_level = 1

    STATE_KEY = "runtime_state"
    EVENT_ACTION = "p115bridge_tg_transfer_success"

    _enabled = False
    _cron = "*/15 * * * *"
    _channels_text = ""
    _cookies = ""
    _cloud_prefix = "/115open"
    _target_path = "/115open/最近接收/TG"
    _auto_create_dir = True
    _first_run_backfill = False
    _minimum_score = 80
    _history_minimum_score = 80
    _max_transfers_per_run = 3
    _max_transfers_per_subscription = 1
    _transfer_delay_seconds = 30
    _p115_cooldown_minutes = 30
    _stop_on_rate_limit = True
    _bridge_enabled = True
    _bridge_delay_seconds = 120
    _request_timeout = 20
    _proxy = ""
    _notify_enabled = False
    _notify_scan_summary = True
    _notify_empty_scan = False
    _notify_match = False
    _notify_transfer_success = True
    _notify_transfer_failure = True
    _dry_run = False

    _history_enabled = True
    _auto_backfill_history = True
    _backfill_transfer_enabled = False
    _history_auto_transfer = True
    _backfill_pages_per_run = 5
    _backfill_resources_per_run = 200
    _backfill_page_delay_seconds = 2
    _history_match_limit = 500
    _retry_limit = 3

    _check_media_exists = True
    _only_missing = True
    _skip_existing_movie = True
    _tv_only_missing_episodes = True
    _auto_transfer_unknown_episode = False
    _skip_low_quality = True
    _log_unmatched = True

    _run_lock = Lock()

    def init_plugin(self, config: Optional[dict] = None):
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._cron = str(config.get("cron") or "*/15 * * * *").strip()
        self._channels_text = str(config.get("channels") or "").strip()
        self._cookies = str(config.get("cookies") or "").strip()
        self._cloud_prefix = normalize_posix_path(str(config.get("cloud_prefix") or "/115open"))
        self._target_path = normalize_posix_path(str(config.get("target_path") or "/115open/最近接收/TG"))
        self._auto_create_dir = bool(config.get("auto_create_dir", True))
        self._first_run_backfill = bool(config.get("first_run_backfill", False))
        self._minimum_score = max(0, int(config.get("minimum_score") or 80))
        self._history_minimum_score = max(0, int(config.get("history_minimum_score") or self._minimum_score))
        self._max_transfers_per_run = max(0, int(config.get("max_transfers_per_run") or 3))
        self._max_transfers_per_subscription = max(0, int(config.get("max_transfers_per_subscription") or 1))
        self._transfer_delay_seconds = max(0, int(config.get("transfer_delay_seconds") or 30))
        self._p115_cooldown_minutes = max(1, int(config.get("p115_cooldown_minutes") or 30))
        self._stop_on_rate_limit = bool(config.get("stop_on_rate_limit", True))
        self._bridge_enabled = bool(config.get("bridge_enabled", True))
        self._bridge_delay_seconds = max(0, int(config.get("bridge_delay_seconds") or 120))
        self._request_timeout = max(5, int(config.get("request_timeout") or 20))
        self._proxy = str(config.get("proxy") or "").strip()
        self._notify_enabled = bool(config.get("notify_enabled", False))
        self._notify_scan_summary = bool(config.get("notify_scan_summary", True))
        self._notify_empty_scan = bool(config.get("notify_empty_scan", False))
        self._notify_match = bool(config.get("notify_match", False))
        self._notify_transfer_success = bool(config.get("notify_transfer_success", True))
        self._notify_transfer_failure = bool(config.get("notify_transfer_failure", True))
        self._dry_run = bool(config.get("dry_run", False))

        self._history_enabled = bool(config.get("history_enabled", True))
        self._auto_backfill_history = bool(config.get("auto_backfill_history", True))
        self._backfill_transfer_enabled = bool(config.get("backfill_transfer_enabled", False))
        self._history_auto_transfer = bool(config.get("history_auto_transfer", True))
        self._backfill_pages_per_run = max(0, int(config.get("backfill_pages_per_run") or 5))
        self._backfill_resources_per_run = max(0, int(config.get("backfill_resources_per_run") or 200))
        self._backfill_page_delay_seconds = max(0, int(config.get("backfill_page_delay_seconds") or 2))
        self._history_match_limit = max(1, int(config.get("history_match_limit") or 500))
        self._retry_limit = max(1, int(config.get("retry_limit") or 3))

        self._check_media_exists = bool(config.get("check_media_exists", True))
        self._only_missing = bool(config.get("only_missing", True))
        self._skip_existing_movie = bool(config.get("skip_existing_movie", True))
        self._tv_only_missing_episodes = bool(config.get("tv_only_missing_episodes", True))
        self._auto_transfer_unknown_episode = bool(config.get("auto_transfer_unknown_episode", False))
        self._skip_low_quality = bool(config.get("skip_low_quality", True))
        self._log_unmatched = bool(config.get("log_unmatched", True))

        self.update_config(self._config_dict())
        logger.info("〖TG115自动转存〗初始化完成 version=%s enabled=%s channels=%s", self.plugin_version, self._enabled, len(self._channels()))

    def _config_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self._enabled,
            "cron": self._cron,
            "channels": self._channels_text,
            "cookies": self._cookies,
            "cloud_prefix": self._cloud_prefix,
            "target_path": self._target_path,
            "auto_create_dir": self._auto_create_dir,
            "first_run_backfill": self._first_run_backfill,
            "minimum_score": self._minimum_score,
            "history_minimum_score": self._history_minimum_score,
            "max_transfers_per_run": self._max_transfers_per_run,
            "max_transfers_per_subscription": self._max_transfers_per_subscription,
            "transfer_delay_seconds": self._transfer_delay_seconds,
            "p115_cooldown_minutes": self._p115_cooldown_minutes,
            "stop_on_rate_limit": self._stop_on_rate_limit,
            "bridge_enabled": self._bridge_enabled,
            "bridge_delay_seconds": self._bridge_delay_seconds,
            "request_timeout": self._request_timeout,
            "proxy": self._proxy,
            "notify_enabled": self._notify_enabled,
            "notify_scan_summary": self._notify_scan_summary,
            "notify_empty_scan": self._notify_empty_scan,
            "notify_match": self._notify_match,
            "notify_transfer_success": self._notify_transfer_success,
            "notify_transfer_failure": self._notify_transfer_failure,
            "dry_run": self._dry_run,
            "history_enabled": self._history_enabled,
            "auto_backfill_history": self._auto_backfill_history,
            "backfill_transfer_enabled": self._backfill_transfer_enabled,
            "history_auto_transfer": self._history_auto_transfer,
            "backfill_pages_per_run": self._backfill_pages_per_run,
            "backfill_resources_per_run": self._backfill_resources_per_run,
            "backfill_page_delay_seconds": self._backfill_page_delay_seconds,
            "history_match_limit": self._history_match_limit,
            "retry_limit": self._retry_limit,
            "check_media_exists": self._check_media_exists,
            "only_missing": self._only_missing,
            "skip_existing_movie": self._skip_existing_movie,
            "tv_only_missing_episodes": self._tv_only_missing_episodes,
            "auto_transfer_unknown_episode": self._auto_transfer_unknown_episode,
            "skip_low_quality": self._skip_low_quality,
            "log_unmatched": self._log_unmatched,
        }

    def get_state(self) -> bool:
        return self._enabled

    def get_service(self) -> List[Dict[str, Any]] | None:
        if not self._enabled:
            return None
        try:
            trigger = CronTrigger.from_crontab(self._cron)
        except Exception as err:
            logger.error("〖TG115自动转存〗Cron 无效: %s", err)
            return None
        return [{
            "id": "Tg115AutoTransfer.scan",
            "name": "TG公开频道增量扫描、历史回填与115受控转存",
            "trigger": trigger,
            "func": self.scan_once,
            "kwargs": {},
        }]

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [
            {"cmd": "/tg115_scan", "event": EventType.PluginAction, "desc": "立即扫描TG频道", "category": "TG115自动转存", "data": {"action": "tg115_scan"}},
            {"cmd": "/tg115_status", "event": EventType.PluginAction, "desc": "查看TG115状态", "category": "TG115自动转存", "data": {"action": "tg115_status"}},
            {"cmd": "/tg115_backfill", "event": EventType.PluginAction, "desc": "继续TG历史回填", "category": "TG115自动转存", "data": {"action": "tg115_backfill"}},
            {"cmd": "/tg115_match", "event": EventType.PluginAction, "desc": "重新匹配历史库", "category": "TG115自动转存", "data": {"action": "tg115_match"}},
            {"cmd": "/tg115_reset", "event": EventType.PluginAction, "desc": "重置TG频道游标", "category": "TG115自动转存", "data": {"action": "tg115_reset"}},
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {"path": "/scan_now", "endpoint": self._api_scan_now, "methods": ["POST"], "auth": "bear", "summary": "立即运行"},
            {"path": "/backfill_now", "endpoint": self._api_backfill_now, "methods": ["POST"], "auth": "bear", "summary": "继续历史回填"},
            {"path": "/match_history", "endpoint": self._api_match_history, "methods": ["POST"], "auth": "bear", "summary": "重新匹配历史库"},
            {"path": "/status", "endpoint": self._api_status, "methods": ["GET"], "auth": "bear", "summary": "获取运行状态"},
            {"path": "/reset", "endpoint": self._api_reset, "methods": ["POST"], "auth": "bear", "summary": "重置频道游标与去重记录"},
            {"path": "/reset_backfill", "endpoint": self._api_reset_backfill, "methods": ["POST"], "auth": "bear", "summary": "重置历史回填进度"},
            {"path": "/clear_history", "endpoint": self._api_clear_history, "methods": ["POST"], "auth": "bear", "summary": "清空历史资源库"},
        ]

    @eventmanager.register(EventType.PluginAction)
    def remote_action(self, event=None):
        if not event or not event.event_data:
            return
        action = event.event_data.get("action")
        if action not in {"tg115_scan", "tg115_status", "tg115_reset", "tg115_backfill", "tg115_match"}:
            return
        channel = event.event_data.get("channel")
        userid = event.event_data.get("user")
        try:
            if action == "tg115_scan":
                result = self.scan_once(source="远程命令")
                title = "TG115自动转存：扫描完成"
                text = self._format_scan_result(result, "远程命令")
            elif action == "tg115_backfill":
                result = self.backfill_once(source="远程命令")
                title, text = "TG115自动转存：历史回填完成", str(result)
            elif action == "tg115_match":
                result = self.match_history_once(source="远程命令")
                title, text = "TG115自动转存：历史重匹配完成", str(result)
            elif action == "tg115_reset":
                self._save_state({})
                title, text = "TG115自动转存：状态已重置", "下次扫描会重新建立频道游标"
            else:
                title, text = "TG115自动转存状态", "\n".join(f"{k}: {v}" for k, v in self._status().items())
            self.post_message(channel=channel, title=title, text=text, userid=userid)
        except Exception as err:
            logger.error("〖TG115自动转存〗远程命令失败: %s", err, exc_info=True)

    def _channels(self) -> list[str]:
        result: list[str] = []
        for line in self._channels_text.replace(",", "\n").splitlines():
            channel = TelegramPublicClient.normalize_channel(line)
            if channel and channel not in result:
                result.append(channel)
        return result

    def _load_state(self) -> dict:
        value = self.get_data(self.STATE_KEY)
        return value if isinstance(value, dict) else {}

    def _save_state(self, state: dict) -> None:
        self.save_data(self.STATE_KEY, state)

    def _store(self) -> Tg115HistoryStore:
        return Tg115HistoryStore(Path("/config/plugins/tg115autotransfer/history.db"))

    def _subscriptions(self) -> list[SubscriptionInfo]:
        try:
            rows = SubscribeOper().list()
        except Exception as err:
            logger.error("〖TG115自动转存〗读取订阅失败: %s", err, exc_info=True)
            return []
        active = [row for row in rows if str(getattr(row, "state", "") or "").upper() not in {"Y", "D", "DONE", "STOP"}]
        return SubscriptionMatcher.from_moviepilot(active)

    def _target_pan_path(self) -> str:
        return cloud_path_to_pan_path(self._target_path, self._cloud_prefix)

    def _send_notification(self, title: str, text: str) -> None:
        if not self._notify_enabled:
            return
        try:
            self.post_message(mtype=NotificationType.Plugin, title=title, text=text)
        except Exception as err:
            logger.error("〖TG115自动转存〗发送通知失败: %s", err, exc_info=True)

    @staticmethod
    def _format_scan_result(result: Dict[str, int], source: str, bridge_notified: bool = False) -> str:
        return (
            f"触发方式：{source}\n"
            f"历史新增：{result.get('history_resources_added', 0)}\n"
            f"链接新增：{result.get('history_links_added', 0)}\n"
            f"新消息：{result.get('new_messages', 0)}\n"
            f"匹配订阅：{result.get('matched', 0)}\n"
            f"媒体库已有跳过：{result.get('existing', 0)}\n"
            f"需要确认：{result.get('need_confirm', 0)}\n"
            f"成功转存：{result.get('transferred', 0)}\n"
            f"演练预览：{result.get('previewed', 0)}\n"
            f"跳过：{result.get('skipped', 0)}\n"
            f"失败：{result.get('errors', 0)}\n"
            f"整理桥接：{'已通知' if bridge_notified else '未触发'}"
        )

    def _notify_scan_result(self, result: Dict[str, int], source: str, bridge_notified: bool) -> None:
        if not self._notify_enabled or not self._notify_scan_summary:
            return
        has_activity = any(int(result.get(key, 0) or 0) > 0 for key in ("new_messages", "matched", "transferred", "previewed", "errors", "existing", "need_confirm"))
        if not has_activity and not self._notify_empty_scan:
            return
        title = "TG115自动转存：扫描完成"
        if result.get("errors", 0):
            title = "TG115自动转存：扫描完成（有失败）"
        elif result.get("transferred", 0):
            title = "TG115自动转存：扫描并转存完成"
        self._send_notification(title, self._format_scan_result(result, source, bridge_notified))

    def _notify_match_result(self, resource: TelegramResource, subscription: Any, score: int) -> None:
        if not self._notify_enabled or not self._notify_match:
            return
        self._send_notification(
            "TG115自动转存：匹配到订阅资源",
            f"订阅：{subscription.name}\nTG标题：{resource.title}\n频道：@{resource.channel}\n消息ID：{resource.message_id}\n匹配分数：{score}\n115链接数：{len(resource.links)}",
        )

    def _notify_transfer_result(self, *, success: bool, resource: TelegramResource, subscription: Any, share_url: str, message: str) -> None:
        if not self._notify_enabled:
            return
        if success and not self._notify_transfer_success:
            return
        if not success and not self._notify_transfer_failure:
            return
        self._send_notification(
            "TG115自动转存：转存成功" if success else "TG115自动转存：转存失败",
            f"订阅：{subscription.name}\nTG标题：{resource.title}\n频道：@{resource.channel}\n消息ID：{resource.message_id}\n目标目录：{self._target_pan_path()}\n分享链接：{share_url}\n结果：{message}",
        )

    def _is_rate_limited(self, err: Exception | str) -> bool:
        text = str(err)
        return any(key in text for key in ("770004", "已达到当前访问上限", "稍后再试", "访问频繁", "rate limit", "too many", "Too Many"))

    def _cooldown_until(self) -> datetime | None:
        value = str(self._load_state().get("p115_cooldown_until") or "")
        if not value:
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    def _in_cooldown(self) -> bool:
        until = self._cooldown_until()
        return bool(until and until > datetime.now())

    def _set_cooldown(self, err: Exception | str) -> None:
        state = self._load_state()
        until = datetime.now() + timedelta(minutes=self._p115_cooldown_minutes)
        state["p115_cooldown_until"] = until.strftime("%Y-%m-%d %H:%M:%S")
        state["p115_last_rate_limit_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state["p115_last_rate_limit_error"] = str(err)[:500]
        self._save_state(state)
        logger.warning("〖TG115自动转存〗检测到115限流，进入冷却 %s 分钟，本轮停止转存：%s", self._p115_cooldown_minutes, err)

    def _media_decision(self, resource: TelegramResource, subscription: SubscriptionInfo) -> tuple[bool, str, str]:
        text = f"{resource.title}\n{resource.text}"
        if self._skip_low_quality and looks_like_low_quality(text):
            return False, STATUS_SKIPPED, "低质量/枪版关键词，跳过"
        if not self._check_media_exists and not self._only_missing:
            return True, STATUS_MATCHED, "未启用媒体库/缺失检查"

        is_tv = "电视" in subscription.media_type or "剧" in subscription.media_type or subscription.season is not None
        episodes = parse_episodes(text)
        season = parse_season(text) or int(subscription.season or 1) if is_tv else None
        lack_episode = subscription.lack_episode

        if is_tv and self._tv_only_missing_episodes:
            if not episodes:
                if self._auto_transfer_unknown_episode:
                    return True, STATUS_NEED_CONFIRM, "无法识别季集，但配置允许自动转存未知集数"
                return False, STATUS_NEED_CONFIRM, "无法识别季集，默认不自动转存"
            missing = self._normalize_lack_episodes(lack_episode)
            if missing and not (episodes & missing):
                return False, STATUS_EXISTING, f"订阅不缺这些集数：S{int(season or 1):02d}E{','.join(str(e) for e in sorted(episodes))}"
            return True, STATUS_MATCHED, f"命中缺失集：S{int(season or 1):02d}E{','.join(str(e) for e in sorted(episodes))}"

        if not is_tv and self._skip_existing_movie:
            state = str(getattr(subscription, "state", "") or "").upper()
            if state in {"Y", "DONE"}:
                return False, STATUS_EXISTING, "电影订阅已完成，跳过"
        return True, STATUS_MATCHED, "媒体库检查通过"

    @staticmethod
    def _normalize_lack_episodes(value: Any) -> set[int]:
        if value is None:
            return set()
        if isinstance(value, (list, set, tuple)):
            result: set[int] = set()
            for item in value:
                try:
                    result.add(int(item))
                except Exception:
                    result.update(parse_episodes(str(item)))
            return result
        if isinstance(value, dict):
            result: set[int] = set()
            for item in value.values():
                result.update(Tg115AutoTransfer._normalize_lack_episodes(item))
            return result
        return parse_episodes(str(value))

    def _record_scan_result(self, result: Dict[str, int], source: str, bridge_notified: bool = False) -> None:
        state = self._load_state()
        stats = state.setdefault("stats", {})
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        stats["runs"] = int(stats.get("runs", 0)) + 1
        stats["last_run"] = now
        stats["transferred"] = int(stats.get("transferred", 0)) + int(result.get("transferred", 0) or 0)
        stats["previewed"] = int(stats.get("previewed", 0)) + int(result.get("previewed", 0) or 0)
        stats["errors"] = int(stats.get("errors", 0)) + int(result.get("errors", 0) or 0)
        state["last_result"] = {**result, "source": source, "time": now, "bridge_notified": bridge_notified}
        self._save_state(state)

    @staticmethod
    def _log_scan_summary(result: Dict[str, int], source: str, bridge_notified: bool = False) -> None:
        logger.info(
            "〖TG115自动转存〗扫描结束：触发方式=%s，历史新增=%s，链接新增=%s，新消息=%s，匹配=%s，已有跳过=%s，需要确认=%s，成功转存=%s，演练=%s，跳过=%s，失败=%s，整理桥接=%s",
            source,
            result.get("history_resources_added", 0),
            result.get("history_links_added", 0),
            result.get("new_messages", 0),
            result.get("matched", 0),
            result.get("existing", 0),
            result.get("need_confirm", 0),
            result.get("transferred", 0),
            result.get("previewed", 0),
            result.get("skipped", 0),
            result.get("errors", 0),
            "已通知" if bridge_notified else "未触发",
        )

    def _empty_result(self) -> Dict[str, int]:
        return {"new_messages": 0, "matched": 0, "transferred": 0, "previewed": 0, "skipped": 0, "errors": 0, "existing": 0, "need_confirm": 0, "history_resources_added": 0, "history_links_added": 0, "backfill_pages": 0}

    def scan_once(self, source: str = "定时任务") -> Dict[str, int]:
        result = self._empty_result()
        logger.info("〖TG115自动转存〗开始运行，触发方式：%s", source)
        if not self._run_lock.acquire(blocking=False):
            result["skipped"] += 1
            logger.warning("〖TG115自动转存〗已有任务正在运行，本次请求跳过")
            self._record_scan_result(result, source)
            return result
        run_id = 0
        bridge_notified = False
        try:
            channels = self._channels()
            store = self._store() if self._history_enabled else None
            if store:
                run_id = store.start_run(source, channels=len(channels))
            if self._auto_backfill_history and store:
                backfill = self.backfill_once(source=source, acquire_lock=False)
                for key in ("history_resources_added", "history_links_added", "backfill_pages", "errors"):
                    result[key] += int(backfill.get(key, 0) or 0)
            increment = self._scan_increment(channels, store)
            for key, value in increment.items():
                result[key] = result.get(key, 0) + int(value or 0)
            if store:
                matched = self.match_history_once(source=source, acquire_lock=False, allow_transfer=self._history_auto_transfer)
                for key, value in matched.items():
                    result[key] = result.get(key, 0) + int(value or 0)
            bridge_notified = result.get("transferred", 0) > 0 and self._bridge_enabled
            if bridge_notified:
                if self._bridge_delay_seconds > 0:
                    logger.info("〖TG115自动转存〗等待 %s 秒后通知115整理入队桥接", self._bridge_delay_seconds)
                    time.sleep(self._bridge_delay_seconds)
                eventmanager.send_event(EventType.PluginAction, {"action": self.EVENT_ACTION, "source": "Tg115AutoTransfer"})
                logger.info("〖TG115自动转存〗已通知115整理入队桥接")
            self._record_scan_result(result, source, bridge_notified)
            self._log_scan_summary(result, source, bridge_notified)
            self._notify_scan_result(result, source, bridge_notified)
            if store and run_id:
                store.finish_run(run_id, **{k: result.get(k, 0) for k in ("backfill_pages", "history_resources_added", "history_links_added", "matched", "transferred", "skipped", "errors")}, summary=self._format_scan_result(result, source, bridge_notified))
            return result
        finally:
            self._run_lock.release()

    def _scan_increment(self, channels: list[str], store: Tg115HistoryStore | None) -> Dict[str, int]:
        result = self._empty_result()
        if not channels:
            logger.warning("〖TG115自动转存〗未配置公开频道")
            return result
        tg_client = TelegramPublicClient(timeout=self._request_timeout, proxy=self._proxy)
        try:
            for channel in channels:
                try:
                    messages = sorted(tg_client.fetch_latest(channel), key=lambda item: item.message_id)
                except Exception as err:
                    logger.error("〖TG115自动转存〗频道 %s 读取失败: %s", channel, err, exc_info=True)
                    result["errors"] += 1
                    continue
                logger.info("〖TG115自动转存〗频道 %s 当前页读取到 %s 条含115链接的消息", channel, len(messages))
                if store:
                    stats = store.upsert_resources(messages)
                    result["history_resources_added"] += stats["resources_added"]
                    result["history_links_added"] += stats["links_added"]
                    newest = max((m.message_id for m in messages), default=0)
                    oldest = min((m.message_id for m in messages), default=0)
                    if newest:
                        store.update_channel_state(channel, newest_id=newest, oldest_id=oldest, last_increment_scan_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                result["new_messages"] += len(messages)
        finally:
            tg_client.close()
        return result

    def backfill_once(self, source: str = "手动回填", acquire_lock: bool = True) -> Dict[str, int]:
        result = self._empty_result()
        if acquire_lock and not self._run_lock.acquire(blocking=False):
            result["skipped"] += 1
            return result
        tg_client = TelegramPublicClient(timeout=self._request_timeout, proxy=self._proxy)
        try:
            if not self._history_enabled:
                logger.warning("〖TG115自动转存〗历史资源库未启用，跳过回填")
                return result
            store = self._store()
            for channel in self._channels():
                state = store.get_channel_state(channel)
                if int(state.get("backfill_complete") or 0):
                    logger.info("〖TG115自动转存〗频道 %s 历史回填已完成，跳过", channel)
                    continue
                before_id = int(state.get("backfill_before_id") or state.get("oldest_id") or 0)
                pages = 0
                saved = 0
                while pages < self._backfill_pages_per_run and saved < self._backfill_resources_per_run:
                    try:
                        messages = sorted(tg_client.fetch_before(channel, before_id), key=lambda item: item.message_id)
                    except Exception as err:
                        logger.error("〖TG115自动转存〗频道 %s 历史回填读取失败: %s", channel, err, exc_info=True)
                        store.update_channel_state(channel, last_error=str(err))
                        result["errors"] += 1
                        break
                    if not messages:
                        store.update_channel_state(channel, backfill_complete=1, last_backfill_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                        logger.info("〖TG115自动转存〗频道 %s 历史回填完成，没有更早的含115链接消息", channel)
                        break
                    stats = store.upsert_resources(messages)
                    result["history_resources_added"] += stats["resources_added"]
                    result["history_links_added"] += stats["links_added"]
                    pages += 1
                    result["backfill_pages"] += 1
                    saved += len(messages)
                    oldest = min(item.message_id for item in messages)
                    newest = max(item.message_id for item in messages)
                    store.update_channel_state(
                        channel,
                        newest_id=max(int(state.get("newest_id") or 0), newest),
                        oldest_id=oldest,
                        backfill_before_id=oldest,
                        last_backfill_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        total_pages=int(state.get("total_pages") or 0) + pages,
                        total_resources=int(state.get("total_resources") or 0) + saved,
                        last_error="",
                    )
                    before_id = oldest
                    logger.info("〖TG115自动转存〗频道 %s 历史回填第 %s 页：资源=%s，新增资源=%s，新增链接=%s，下次before=%s", channel, pages, len(messages), stats["resources_added"], stats["links_added"], before_id)
                    if self._backfill_page_delay_seconds > 0:
                        time.sleep(self._backfill_page_delay_seconds)
            return result
        finally:
            tg_client.close()
            if acquire_lock:
                self._run_lock.release()

    def match_history_once(self, source: str = "历史重匹配", acquire_lock: bool = True, allow_transfer: bool = True) -> Dict[str, int]:
        result = self._empty_result()
        if acquire_lock and not self._run_lock.acquire(blocking=False):
            result["skipped"] += 1
            return result
        try:
            if not self._history_enabled:
                logger.warning("〖TG115自动转存〗历史资源库未启用，跳过历史匹配")
                return result
            store = self._store()
            subscriptions = self._subscriptions()
            matcher = SubscriptionMatcher(self._history_minimum_score)
            links = store.pending_links(limit=self._history_match_limit, retry_limit=self._retry_limit)
            logger.info("〖TG115自动转存〗开始匹配历史库：候选链接=%s，活动订阅=%s", len(links), len(subscriptions))
            transfer_client: P115TransferClient | None = None
            target_cid: str | None = None
            transfer_count_by_sub: Dict[int, int] = {}
            last_transfer_at = 0.0
            cooldown = self._in_cooldown()
            for link in links:
                resource = TelegramResource(
                    channel=str(link["channel"]),
                    message_id=int(link["message_id"]),
                    title=str(link["resource_title"] or ""),
                    text=str(link["resource_text"] or ""),
                    published_at=str(link["resource_published_at"] or ""),
                    message_url=str(link["resource_message_url"] or ""),
                    links=[ShareLink(url=str(link["url"]), share_code=str(link["share_code"]), receive_code=str(link["receive_code"] or ""))],
                    content_hash=str(link["resource_content_hash"] or ""),
                )
                match = matcher.match(resource, subscriptions)
                if not match.subscription or match.score < self._history_minimum_score:
                    if self._log_unmatched:
                        logger.info("〖TG115自动转存〗历史未匹配：频道=%s，消息ID=%s，标题=%s，最高分=%s，最低要求=%s", resource.channel, resource.message_id, resource.title, match.score, self._history_minimum_score)
                    result["skipped"] += 1
                    continue
                allowed, status, reason = self._media_decision(resource, match.subscription)
                if not allowed:
                    store.update_link_status(int(link["id"]), status, matched_subscription_id=match.subscription.sid, matched_subscription_name=match.subscription.name, matched_score=match.score, matched_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), error_message=reason)
                    if status == STATUS_EXISTING:
                        result["existing"] += 1
                    elif status == STATUS_NEED_CONFIRM:
                        result["need_confirm"] += 1
                    else:
                        result["skipped"] += 1
                    logger.info("〖TG115自动转存〗历史命中但不转存：%s -> %s，分数=%s，原因=%s", resource.title, match.subscription.name, match.score, reason)
                    continue
                result["matched"] += 1
                store.update_link_status(int(link["id"]), STATUS_MATCHED, matched_subscription_id=match.subscription.sid, matched_subscription_name=match.subscription.name, matched_score=match.score, matched_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), error_message=reason)
                logger.info("〖TG115自动转存〗历史命中：%s -> %s，分数=%s，原因=%s", resource.title, match.subscription.name, match.score, reason)
                if not allow_transfer or not self._history_auto_transfer or self._dry_run:
                    result["previewed"] += 1
                    continue
                if cooldown or self._in_cooldown():
                    logger.warning("〖TG115自动转存〗115冷却中，跳过本轮真实转存")
                    result["skipped"] += 1
                    break
                if result["transferred"] >= self._max_transfers_per_run:
                    logger.warning("〖TG115自动转存〗达到单轮最多转存数量 %s，本轮停止转存", self._max_transfers_per_run)
                    break
                sub_count = transfer_count_by_sub.get(match.subscription.sid, 0)
                if self._max_transfers_per_subscription and sub_count >= self._max_transfers_per_subscription:
                    logger.info("〖TG115自动转存〗订阅 %s 已达到本轮上限 %s，跳过", match.subscription.name, self._max_transfers_per_subscription)
                    result["skipped"] += 1
                    continue
                if self._transfer_delay_seconds > 0 and last_transfer_at > 0:
                    wait = self._transfer_delay_seconds - (time.time() - last_transfer_at)
                    if wait > 0:
                        logger.info("〖TG115自动转存〗等待 %.1f 秒后继续下一次转存，避免触发115限流", wait)
                        time.sleep(wait)
                try:
                    if transfer_client is None:
                        transfer_client = P115TransferClient(self._cookies, auto_create=self._auto_create_dir)
                    if target_cid is None:
                        target_cid = transfer_client.resolve_path(self._target_pan_path())
                        logger.info("〖TG115自动转存〗本轮目标目录CID已解析并缓存：%s -> %s", self._target_pan_path(), target_cid)
                    share = ShareLink(url=str(link["url"]), share_code=str(link["share_code"]), receive_code=str(link["receive_code"] or ""))
                    transfer = transfer_client.receive(share, target_cid)
                    if not transfer.success:
                        raise RuntimeError(transfer.message)
                    store.update_link_status(int(link["id"]), STATUS_TRANSFERRED, transferred_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), target_cid=target_cid, error_message=transfer.message)
                    result["transferred"] += 1
                    transfer_count_by_sub[match.subscription.sid] = sub_count + 1
                    last_transfer_at = time.time()
                    logger.info("〖TG115自动转存〗转存成功：%s -> %s，结果=%s", link["url"], self._target_pan_path(), transfer.message)
                    self._notify_transfer_result(success=True, resource=resource, subscription=match.subscription, share_url=str(link["url"]), message=transfer.message)
                except Exception as err:
                    result["errors"] += 1
                    store.increment_retry(int(link["id"]), str(err))
                    logger.error("〖TG115自动转存〗转存失败 %s: %s", link["url"], err, exc_info=True)
                    if self._is_rate_limited(err):
                        self._set_cooldown(err)
                        if self._stop_on_rate_limit:
                            break
            return result
        finally:
            if acquire_lock:
                self._run_lock.release()

    def _status(self) -> dict:
        state = self._load_state()
        stats = state.get("stats") or {}
        history = self._store().stats() if self._history_enabled else {}
        cooldown_until = self._cooldown_until()
        return {
            "enabled": self._enabled,
            "version": self.plugin_version,
            "channels": len(self._channels()),
            "cron": self._cron,
            "target_cloud_path": self._target_path,
            "target_pan_path": self._target_pan_path(),
            "minimum_score": self._minimum_score,
            "history_minimum_score": self._history_minimum_score,
            "history_enabled": self._history_enabled,
            "history_resources": history.get("resources", 0),
            "history_links": history.get("links", 0),
            "history_by_status": history.get("by_status", {}),
            "p115_cooldown_until": cooldown_until.strftime("%Y-%m-%d %H:%M:%S") if cooldown_until else "-",
            "p115_cooldown_active": self._in_cooldown(),
            "bridge_enabled": self._bridge_enabled,
            "notify_enabled": self._notify_enabled,
            "processed_shares": len(state.get("processed_shares") or {}),
            "last_run": stats.get("last_run") or "-",
            "transferred": stats.get("transferred", 0),
            "previewed": stats.get("previewed", 0),
            "errors": stats.get("errors", 0),
        }

    async def _api_scan_now(self) -> dict:
        logger.info("〖TG115自动转存〗收到手动立即运行请求")
        try:
            result = self.scan_once(source="手动运行")
            return {"code": 0, "message": "运行完成，请查看插件日志和最近一次运行", "data": result}
        except Exception as err:
            logger.error("〖TG115自动转存〗手动运行失败: %s", err, exc_info=True)
            return {"code": 1, "message": str(err), "data": None}

    async def _api_backfill_now(self) -> dict:
        logger.info("〖TG115自动转存〗收到继续历史回填请求")
        return {"code": 0, "message": "历史回填完成", "data": self.backfill_once(source="手动历史回填")}

    async def _api_match_history(self) -> dict:
        logger.info("〖TG115自动转存〗收到重新匹配历史库请求")
        return {"code": 0, "message": "历史库匹配完成", "data": self.match_history_once(source="手动历史匹配")}

    async def _api_status(self) -> dict:
        return {"code": 0, "message": "success", "data": self._status()}

    async def _api_reset(self) -> dict:
        logger.warning("〖TG115自动转存〗收到手动重置游标与去重记录请求")
        self._save_state({})
        return {"code": 0, "message": "状态已重置，下次扫描会重新建立频道游标", "data": None}

    async def _api_reset_backfill(self) -> dict:
        logger.warning("〖TG115自动转存〗收到重置历史回填进度请求")
        self._store().reset_backfill(self._channels())
        return {"code": 0, "message": "历史回填进度已重置，历史资源库未清空", "data": None}

    async def _api_clear_history(self) -> dict:
        logger.warning("〖TG115自动转存〗收到清空历史资源库请求")
        self._store().clear_all()
        return {"code": 0, "message": "历史资源库已清空", "data": None}

    def stop_service(self):
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [{
            "component": "VForm",
            "content": [
                {"component": "VAlert", "props": {"type": "success", "variant": "tonal", "text": "v0.4.1 更新：这版主要把页面说明改成人话。每个按钮、每组选项都写清楚是干嘛的；功能逻辑沿用 v0.4.0：TG历史库、115限流保护、媒体库/缺失集检查。"}},
                {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": "这个插件的用途：先把公开TG频道里的115分享保存到本地历史库；以后你在MP新增订阅时，它可以拿历史库重新匹配。真正转存前会尽量判断媒体库是不是已经有、剧集是不是缺，并按你设置的间隔慢慢转，避免115限流。"}},
                {"component": "VAlert", "props": {"type": "warning", "variant": "tonal", "text": "建议第一次使用：先打开“仅日志演练”，让它只写日志不真实转存；确认匹配没问题后，再关闭演练。历史回填默认只入库，不建议一上来就批量真实转存。"}},

                {"component": "VDivider", "props": {"class": "my-3"}},
                {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": "一、基础开关：控制插件是否运行、是否只演练、是否通知整理桥接。"}},
                {"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件", "hint": "关掉后定时任务不会跑。", "persistent-hint": True}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "dry_run", "label": "仅日志演练", "hint": "只看会匹配什么，不真的转存。测试时建议先开。", "persistent-hint": True}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "bridge_enabled", "label": "通知整理桥接", "hint": "转存成功后，让115整理桥接去整理入库。", "persistent-hint": True}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "first_run_backfill", "label": "首次回补当前页", "hint": "只回补TG当前页，不等于全历史。全历史看下面历史库设置。", "persistent-hint": True}}]},
                ]},
                {"component": "VTextarea", "props": {"model": "channels", "label": "公开TG频道", "rows": 5, "placeholder": "每行一个，例如：\nQukanMovie\nhttps://t.me/gimy115\n@gimy115", "hint": "只支持公开频道。插件会读取 t.me/s/频道名 页面。", "persistent-hint": True}},
                {"component": "VTextField", "props": {"model": "cookies", "label": "115 Cookie", "type": "password", "placeholder": "UID=...; CID=...; SEID=...; KID=...", "hint": "真实转存必须填。只演练时可以不填。", "persistent-hint": True}},
                {"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextField", "props": {"model": "cloud_prefix", "label": "CloudDrive2 前缀", "placeholder": "/115open", "hint": "你在MP里看到的115挂载前缀。", "persistent-hint": True}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextField", "props": {"model": "target_path", "label": "转存目标路径", "placeholder": "/115open/最近接收/TG", "hint": "插件会把115分享转存到这里。", "persistent-hint": True}}]},
                ]},

                {"component": "VDivider", "props": {"class": "my-3"}},
                {"component": "VAlert", "props": {"type": "warning", "variant": "tonal", "text": "二、115防限流：这里决定转存速度。想稳，就慢一点。你之前触发过115访问上限，所以建议保持默认保守值。"}},
                {"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "max_transfers_per_run", "label": "单轮最多转存", "type": "number", "hint": "一次运行最多真转几个。建议3以内。", "persistent-hint": True}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "max_transfers_per_subscription", "label": "每个订阅最多转存", "type": "number", "hint": "避免一个订阅一次转太多。建议1。", "persistent-hint": True}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "transfer_delay_seconds", "label": "两次转存间隔秒数", "type": "number", "hint": "每转一个等多久再转下一个。建议30-60秒。", "persistent-hint": True}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "p115_cooldown_minutes", "label": "限流后冷却分钟", "type": "number", "hint": "遇到115访问上限后暂停多久。建议30分钟。", "persistent-hint": True}}]},
                ]},
                {"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "stop_on_rate_limit", "label": "限流就立刻停", "hint": "看到115限流后，本轮不再继续转。建议开启。", "persistent-hint": True}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": "bridge_delay_seconds", "label": "转存后多久通知整理", "type": "number", "hint": "转完后等一会儿再让整理桥接访问115，减少叠加请求。建议120秒。", "persistent-hint": True}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": "request_timeout", "label": "请求超时秒数", "type": "number", "hint": "TG/115请求超过多久算失败。默认20秒。", "persistent-hint": True}}]},
                ]},

                {"component": "VDivider", "props": {"class": "my-3"}},
                {"component": "VAlert", "props": {"type": "success", "variant": "tonal", "text": "三、TG历史库：把TG以前发过的115链接慢慢保存下来。以后你新加老电影/老剧订阅，就可以重新匹配这些历史资源。"}},
                {"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "history_enabled", "label": "启用历史库", "hint": "把TG资源存到本地SQLite库。建议开启。", "persistent-hint": True}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "auto_backfill_history", "label": "自动回填历史", "hint": "每次运行顺手往前翻几页TG历史。", "persistent-hint": True}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "backfill_transfer_enabled", "label": "回填时就转存", "hint": "不建议开启。历史回填可能命中很多资源，容易限流。", "persistent-hint": True}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "history_auto_transfer", "label": "历史命中自动转存", "hint": "重新匹配历史库时，命中且确认缺失才会转。", "persistent-hint": True}}]},
                ]},
                {"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "backfill_pages_per_run", "label": "每轮翻几页TG历史", "type": "number", "hint": "越大越快，但越容易被TG限制。建议5。", "persistent-hint": True}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "backfill_resources_per_run", "label": "每轮最多保存几条", "type": "number", "hint": "防止一次塞太多历史资源。建议200。", "persistent-hint": True}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "backfill_page_delay_seconds", "label": "翻页间隔秒数", "type": "number", "hint": "每翻一页TG历史等多久。建议2秒以上。", "persistent-hint": True}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "history_match_limit", "label": "每轮最多匹配几条", "type": "number", "hint": "重新匹配历史库时，一次最多看多少条。建议500。", "persistent-hint": True}}]},
                ]},

                {"component": "VDivider", "props": {"class": "my-3"}},
                {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": "四、避免重复转存：尽量判断媒体库里有没有、订阅是不是还缺。判断不出来的剧集，默认不自动转，避免乱转。"}},
                {"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "check_media_exists", "label": "转存前先检查", "hint": "先判断是不是已存在或不缺。建议开启。", "persistent-hint": True}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "only_missing", "label": "只转缺失内容", "hint": "订阅不缺的就不转。建议开启。", "persistent-hint": True}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "skip_existing_movie", "label": "电影已有就跳过", "hint": "电影订阅已经完成时，不再转。", "persistent-hint": True}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "tv_only_missing_episodes", "label": "剧集只补缺集", "hint": "能识别出集数时，只转订阅缺的集。", "persistent-hint": True}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "auto_transfer_unknown_episode", "label": "识别不出集数也转", "hint": "风险较高，可能重复转。默认关闭。", "persistent-hint": True}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "skip_low_quality", "label": "跳过枪版低质", "hint": "标题里有CAM/TC/TS/枪版等关键词就跳过。", "persistent-hint": True}}]},
                ]},
                {"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": "minimum_score", "label": "当前页匹配分", "type": "number", "hint": "TG当前页资源达到多少分才算命中。默认80。", "persistent-hint": True}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": "history_minimum_score", "label": "历史库匹配分", "type": "number", "hint": "历史资源达到多少分才算命中。默认80。", "persistent-hint": True}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": "retry_limit", "label": "失败重试次数", "type": "number", "hint": "同一个链接失败几次后不再反复试。默认3。", "persistent-hint": True}}]},
                ]},

                {"component": "VDivider", "props": {"class": "my-3"}},
                {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": "五、定时和通知：定时任务会按Cron运行。通知走MP现有通知渠道。"}},
                {"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextField", "props": {"model": "cron", "label": "多久自动跑一次", "placeholder": "*/15 * * * *", "hint": "默认每15分钟一次。", "persistent-hint": True}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextField", "props": {"model": "proxy", "label": "TG代理（可选）", "placeholder": "http://127.0.0.1:7890", "hint": "访问TG慢或失败时再填。", "persistent-hint": True}}]},
                ]},
                {"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "notify_enabled", "label": "发MP通知", "hint": "用MP已经配置好的通知渠道。", "persistent-hint": True}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "notify_scan_summary", "label": "发运行总结", "hint": "每次运行后发一条结果。", "persistent-hint": True}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "notify_empty_scan", "label": "没结果也通知", "hint": "没匹配到也发通知。一般不用开。", "persistent-hint": True}}]},
                ]},
            ],
        }], self._config_dict()

    def get_page(self) -> Optional[List[dict]]:
        status = self._status()
        state = self._load_state()
        last_result = state.get("last_result") or {}
        history_stats = self._store().stats() if self._history_enabled else {"by_status": {}, "channels": [], "last_runs": []}
        by_status = history_stats.get("by_status", {}) or {}
        last_result_text = "还没有运行记录。你可以先点“立即运行”，建议先打开配置里的“仅日志演练”。"
        if last_result:
            last_result_text = (
                f"时间：{last_result.get('time', '-')} ｜ 方式：{last_result.get('source', '-')} ｜ "
                f"历史新增：{last_result.get('history_resources_added', 0)} ｜ 链接新增：{last_result.get('history_links_added', 0)} ｜ "
                f"匹配：{last_result.get('matched', 0)} ｜ 已有跳过：{last_result.get('existing', 0)} ｜ "
                f"需确认：{last_result.get('need_confirm', 0)} ｜ 转存：{last_result.get('transferred', 0)} ｜ 失败：{last_result.get('errors', 0)}"
            )
        channel_lines = []
        for item in history_stats.get("channels", []):
            channel_lines.append(
                f"{item.get('channel')}：最新ID {item.get('newest_id')}，已回填到 {item.get('oldest_id')}，是否完成：{'是' if item.get('backfill_complete') else '否'}，累计保存 {item.get('total_resources')} 条"
            )
        channel_text = "\n".join(channel_lines) or "还没有历史回填记录。点“继续历史回填”后这里会显示每个频道的进度。"
        cooldown_text = "正常"
        if status.get("p115_cooldown_active"):
            cooldown_text = f"冷却中，到 {status.get('p115_cooldown_until')} 之前不会继续真实转存"
        status_text = (
            f"插件版本：{self.plugin_version} ｜ "
            f"启用：{'是' if self._enabled else '否'} ｜ "
            f"频道数：{len(self._channels())} ｜ "
            f"历史库：{'开启' if self._history_enabled else '关闭'} ｜ "
            f"115状态：{cooldown_text} ｜ "
            f"目标目录：{self._target_pan_path()}"
        )
        history_text = (
            f"资源：{history_stats.get('resources', 0)} ｜ "
            f"链接：{history_stats.get('links', 0)} ｜ "
            f"待匹配：{by_status.get(STATUS_PENDING, 0)} ｜ "
            f"已匹配：{by_status.get(STATUS_MATCHED, 0)} ｜ "
            f"已转存：{by_status.get(STATUS_TRANSFERRED, 0)} ｜ "
            f"媒体库已有跳过：{by_status.get(STATUS_EXISTING, 0)} ｜ "
            f"需要确认：{by_status.get(STATUS_NEED_CONFIRM, 0)} ｜ "
            f"失败：{by_status.get(STATUS_FAILED, 0)}"
        )
        return [
            {"component": "VAlert", "props": {"type": "success", "variant": "tonal", "text": "v0.4.1 更新：这版主要优化页面说明，把按钮和配置项都改成更直白的中文；功能仍是历史资源库、115防限流、媒体库/缺失检查。"}},
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": "怎么用：1）先配置TG频道和115 Cookie；2）先打开“仅日志演练”；3）点“继续历史回填”把TG旧资源存进库；4）新增MP订阅后点“重新匹配历史库”；5）确认日志没问题后再关闭演练真实转存。"}},
            {"component": "VAlert", "props": {"type": "warning", "variant": "tonal", "text": "注意：不要一开始就批量真实转存。115容易限流，建议单轮最多3个、两次转存间隔30秒以上。"}},

            {"component": "VCard", "props": {"variant": "outlined", "class": "mb-3"}, "content": [
                {"component": "VCardTitle", "text": "按钮说明"},
                {"component": "VCardText", "text": "立即运行：执行一次完整流程（扫当前页、按配置回填一点历史、匹配历史库、符合条件才转存）。\n继续历史回填：只往前翻TG历史，把115链接存进本地库，一般不直接转存。\n重新匹配历史库：你新增MP订阅后点这个，用已有历史资源重新找匹配。\n重置游标：让当前页增量扫描重新建立游标，不会清空历史库。\n重置历史回填进度：让历史回填从当前页重新往前翻，已保存的历史资源还在。\n清空历史资源库：删除本地保存的TG历史资源，慎用。"},
            ]},

            {"component": "VRow", "props": {"class": "my-2"}, "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VBtn", "props": {"color": "primary", "block": True, "prepend-icon": "mdi-play", "text": "立即运行"}, "events": {"click": {"api": "plugin/Tg115AutoTransfer/scan_now", "method": "post"}}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VBtn", "props": {"color": "info", "variant": "tonal", "block": True, "prepend-icon": "mdi-history", "text": "继续历史回填"}, "events": {"click": {"api": "plugin/Tg115AutoTransfer/backfill_now", "method": "post"}}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VBtn", "props": {"color": "success", "variant": "tonal", "block": True, "prepend-icon": "mdi-magnify-scan", "text": "重新匹配历史库"}, "events": {"click": {"api": "plugin/Tg115AutoTransfer/match_history", "method": "post"}}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VBtn", "props": {"color": "warning", "variant": "tonal", "block": True, "prepend-icon": "mdi-backup-restore", "text": "重置游标"}, "events": {"click": {"api": "plugin/Tg115AutoTransfer/reset", "method": "post"}}}]},
            ]},
            {"component": "VRow", "props": {"class": "my-2"}, "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VBtn", "props": {"color": "warning", "variant": "outlined", "block": True, "prepend-icon": "mdi-rewind", "text": "重置历史回填进度"}, "events": {"click": {"api": "plugin/Tg115AutoTransfer/reset_backfill", "method": "post"}}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VBtn", "props": {"color": "error", "variant": "outlined", "block": True, "prepend-icon": "mdi-delete-alert", "text": "清空历史资源库"}, "events": {"click": {"api": "plugin/Tg115AutoTransfer/clear_history", "method": "post"}}}]},
            ]},

            {"component": "VCard", "props": {"variant": "outlined", "class": "mb-2"}, "content": [
                {"component": "VCardTitle", "text": "当前状态"},
                {"component": "VCardText", "text": status_text},
            ]},
            {"component": "VCard", "props": {"variant": "outlined", "class": "mb-2"}, "content": [
                {"component": "VCardTitle", "text": "历史库里现在有什么"},
                {"component": "VCardText", "text": history_text},
            ]},
            {"component": "VCard", "props": {"variant": "outlined", "class": "mb-2"}, "content": [
                {"component": "VCardTitle", "text": "每个TG频道回填到哪里了"},
                {"component": "VCardText", "text": channel_text},
            ]},
            {"component": "VCard", "props": {"variant": "outlined"}, "content": [
                {"component": "VCardTitle", "text": "最近一次运行结果"},
                {"component": "VCardText", "text": last_result_text},
            ]},
        ]
