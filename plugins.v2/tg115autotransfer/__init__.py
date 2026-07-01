from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock, Timer
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger

from app.core.event import Event, eventmanager
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType

from .follow_schedule import calculate_next_run, parse_follow_schedule_text, search_web_for_follow_schedule, weekday_text
from .matcher import SubscriptionMatcher
from .models import SubscriptionInfo, TelegramResource
from .records import Tg115StateStore
from .searcher import TgDirectSearcher
from .text import cloud_path_to_pan_path, normalize_posix_path
from .transfer import TransferController


class Tg115AutoTransfer(_PluginBase):
    plugin_name = "TG 115自动转存"
    plugin_desc = "按MoviePilot订阅名直接搜索TG公开频道，命中115分享后去重、判断缺失集并受控转存"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/cloud.png"
    plugin_version = "0.6.0"
    plugin_author = "outxool"
    author_url = "https://github.com/outxool"
    plugin_config_prefix = "tg115autotransfer_"
    plugin_order = 2
    auth_level = 1

    STATE_KEY = "runtime_state"
    EVENT_ACTION = "p115bridge_tg_transfer_success"

    _enabled = False
    _dry_run = True
    _channels_text = ""
    _cookies = ""
    _cloud_prefix = "/115open"
    _target_path = "/115open/最近接收/TG"
    _auto_create_dir = True
    _proxy = ""
    _request_timeout = 20

    _auto_search_on_subscribe = True
    _subscribe_search_delay_seconds = 30
    _scheduled_search_enabled = False
    _cron = "0 */2 * * *"
    _max_subscriptions_per_run = 20
    _max_keywords_per_subscription = 3
    _tg_search_interval_seconds = 2
    _search_retry_limit = 1

    _minimum_score = 80
    _check_media_exists = True
    _only_missing = True
    _skip_existing_movie = True
    _tv_only_missing_episodes = True
    _auto_transfer_unknown_episode = False
    _skip_low_quality = True
    _quality_filter_enabled = True
    _prefer_4k = True
    _min_resolution = "1080p"
    _skip_bdmv_structure = True
    _custom_skip_structure_keywords = ""
    _max_quality_probe_per_subscription = 5
    _allow_unknown_quality = False
    _quality_score_threshold = 40
    _retry_limit = 3

    _max_transfers_per_run = 3
    _max_transfers_per_subscription = 1
    _transfer_delay_seconds = 30
    _p115_cooldown_minutes = 30
    _stop_on_rate_limit = True
    _bridge_enabled = True
    _bridge_delay_seconds = 120

    _follow_enabled = False
    _follow_manual_parse_enabled = True
    _follow_web_lookup_enabled = False
    _follow_delay_minutes = 35
    _follow_max_transfers_per_run = 1
    _follow_daily_trigger_limit = 2
    _follow_web_max_per_run = 3

    _notify_enabled = False
    _notify_scan_summary = True
    _notify_empty_scan = False

    _run_lock = Lock()
    _subscribe_timers: Dict[int, Timer] = {}

    def init_plugin(self, config: Optional[dict] = None):
        self._subscribe_timers = {}
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._dry_run = bool(config.get("dry_run", True))
        self._channels_text = str(config.get("channels") or "").strip()
        self._cookies = str(config.get("cookies") or "").strip()
        self._cloud_prefix = normalize_posix_path(str(config.get("cloud_prefix") or "/115open"))
        self._target_path = normalize_posix_path(str(config.get("target_path") or "/115open/最近接收/TG"))
        self._auto_create_dir = bool(config.get("auto_create_dir", True))
        self._proxy = str(config.get("proxy") or "").strip()
        self._request_timeout = max(5, int(config.get("request_timeout") or 20))

        self._auto_search_on_subscribe = bool(config.get("auto_search_on_subscribe", True))
        self._subscribe_search_delay_seconds = max(0, int(config.get("subscribe_search_delay_seconds") or 30))
        self._scheduled_search_enabled = bool(config.get("scheduled_search_enabled", False))
        self._cron = str(config.get("cron") or "0 */2 * * *").strip()
        self._max_subscriptions_per_run = max(1, int(config.get("max_subscriptions_per_run") or 20))
        self._max_keywords_per_subscription = max(1, int(config.get("max_keywords_per_subscription") or 3))
        self._tg_search_interval_seconds = max(0, int(config.get("tg_search_interval_seconds") or 2))
        self._search_retry_limit = max(1, int(config.get("search_retry_limit") or 1))

        self._minimum_score = max(0, int(config.get("minimum_score") or 80))
        self._check_media_exists = bool(config.get("check_media_exists", True))
        self._only_missing = bool(config.get("only_missing", True))
        self._skip_existing_movie = bool(config.get("skip_existing_movie", True))
        self._tv_only_missing_episodes = bool(config.get("tv_only_missing_episodes", True))
        self._auto_transfer_unknown_episode = bool(config.get("auto_transfer_unknown_episode", False))
        self._skip_low_quality = bool(config.get("skip_low_quality", True))
        self._quality_filter_enabled = bool(config.get("quality_filter_enabled", True))
        self._prefer_4k = bool(config.get("prefer_4k", True))
        self._min_resolution = str(config.get("min_resolution") or "1080p").strip()
        self._skip_bdmv_structure = bool(config.get("skip_bdmv_structure", True))
        self._custom_skip_structure_keywords = str(config.get("custom_skip_structure_keywords") or "").strip()
        self._max_quality_probe_per_subscription = max(0, int(config.get("max_quality_probe_per_subscription") or 5))
        self._allow_unknown_quality = bool(config.get("allow_unknown_quality", False))
        self._quality_score_threshold = max(0, int(config.get("quality_score_threshold") or 40))
        self._retry_limit = max(1, int(config.get("retry_limit") or 3))

        self._max_transfers_per_run = max(0, int(config.get("max_transfers_per_run") or 3))
        self._max_transfers_per_subscription = max(0, int(config.get("max_transfers_per_subscription") or 1))
        self._transfer_delay_seconds = max(0, int(config.get("transfer_delay_seconds") or 30))
        self._p115_cooldown_minutes = max(1, int(config.get("p115_cooldown_minutes") or 30))
        self._stop_on_rate_limit = bool(config.get("stop_on_rate_limit", True))
        self._bridge_enabled = bool(config.get("bridge_enabled", True))
        self._bridge_delay_seconds = max(0, int(config.get("bridge_delay_seconds") or 120))

        self._follow_enabled = bool(config.get("follow_enabled", False))
        self._follow_manual_parse_enabled = bool(config.get("follow_manual_parse_enabled", True))
        self._follow_web_lookup_enabled = bool(config.get("follow_web_lookup_enabled", False))
        self._follow_delay_minutes = max(0, int(config.get("follow_delay_minutes") or 35))
        self._follow_max_transfers_per_run = max(1, int(config.get("follow_max_transfers_per_run") or 1))
        self._follow_daily_trigger_limit = max(1, int(config.get("follow_daily_trigger_limit") or 2))
        self._follow_web_max_per_run = max(0, int(config.get("follow_web_max_per_run") or 3))

        self._notify_enabled = bool(config.get("notify_enabled", False))
        self._notify_scan_summary = bool(config.get("notify_scan_summary", True))
        self._notify_empty_scan = bool(config.get("notify_empty_scan", False))

        self.update_config(self._config_dict())
        logger.info("〖TG115自动转存〗初始化完成 version=%s enabled=%s channels=%s direct_search=True", self.plugin_version, self._enabled, len(self._channels()))

    def _config_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self._enabled,
            "dry_run": self._dry_run,
            "channels": self._channels_text,
            "cookies": self._cookies,
            "cloud_prefix": self._cloud_prefix,
            "target_path": self._target_path,
            "auto_create_dir": self._auto_create_dir,
            "proxy": self._proxy,
            "request_timeout": self._request_timeout,
            "auto_search_on_subscribe": self._auto_search_on_subscribe,
            "subscribe_search_delay_seconds": self._subscribe_search_delay_seconds,
            "scheduled_search_enabled": self._scheduled_search_enabled,
            "cron": self._cron,
            "max_subscriptions_per_run": self._max_subscriptions_per_run,
            "max_keywords_per_subscription": self._max_keywords_per_subscription,
            "tg_search_interval_seconds": self._tg_search_interval_seconds,
            "search_retry_limit": self._search_retry_limit,
            "minimum_score": self._minimum_score,
            "check_media_exists": self._check_media_exists,
            "only_missing": self._only_missing,
            "skip_existing_movie": self._skip_existing_movie,
            "tv_only_missing_episodes": self._tv_only_missing_episodes,
            "auto_transfer_unknown_episode": self._auto_transfer_unknown_episode,
            "skip_low_quality": self._skip_low_quality,
            "quality_filter_enabled": self._quality_filter_enabled,
            "prefer_4k": self._prefer_4k,
            "min_resolution": self._min_resolution,
            "skip_bdmv_structure": self._skip_bdmv_structure,
            "custom_skip_structure_keywords": self._custom_skip_structure_keywords,
            "max_quality_probe_per_subscription": self._max_quality_probe_per_subscription,
            "allow_unknown_quality": self._allow_unknown_quality,
            "quality_score_threshold": self._quality_score_threshold,
            "retry_limit": self._retry_limit,
            "max_transfers_per_run": self._max_transfers_per_run,
            "max_transfers_per_subscription": self._max_transfers_per_subscription,
            "transfer_delay_seconds": self._transfer_delay_seconds,
            "p115_cooldown_minutes": self._p115_cooldown_minutes,
            "stop_on_rate_limit": self._stop_on_rate_limit,
            "bridge_enabled": self._bridge_enabled,
            "bridge_delay_seconds": self._bridge_delay_seconds,
            "follow_enabled": self._follow_enabled,
            "follow_manual_parse_enabled": self._follow_manual_parse_enabled,
            "follow_web_lookup_enabled": self._follow_web_lookup_enabled,
            "follow_delay_minutes": self._follow_delay_minutes,
            "follow_max_transfers_per_run": self._follow_max_transfers_per_run,
            "follow_daily_trigger_limit": self._follow_daily_trigger_limit,
            "follow_web_max_per_run": self._follow_web_max_per_run,
            "notify_enabled": self._notify_enabled,
            "notify_scan_summary": self._notify_scan_summary,
            "notify_empty_scan": self._notify_empty_scan,
        }

    def get_state(self) -> bool:
        return self._enabled

    def get_service(self) -> List[Dict[str, Any]] | None:
        if not self._enabled:
            return None
        services: list[dict] = []
        if self._scheduled_search_enabled:
            try:
                trigger = CronTrigger.from_crontab(self._cron)
            except Exception as err:
                logger.error("〖TG115自动转存〗定时表达式无效: %s", err)
                trigger = None
            if trigger:
                services.append({"id": "Tg115AutoTransfer.search_all", "name": "TG115按订阅直搜", "trigger": trigger, "func": self.search_all_now, "kwargs": {"source": "定时直搜"}})
        if self._follow_enabled:
            services.append({"id": "Tg115AutoTransfer.follow_due", "name": "TG115追更到点直搜", "trigger": CronTrigger.from_crontab("*/5 * * * *"), "func": self.follow_scan_due, "kwargs": {}})
        return services or None

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {"path": "/search_all_now", "endpoint": self._api_search_all_now, "methods": ["POST"], "auth": "bear", "summary": "立即搜索全部订阅"},
            {"path": "/search_recent_subscribe", "endpoint": self._api_search_recent_subscribe, "methods": ["POST"], "auth": "bear", "summary": "搜索最近新增订阅"},
            {"path": "/search_subscription/{subscribe_id}", "endpoint": self._api_search_subscription, "methods": ["POST"], "auth": "bear", "summary": "搜索指定订阅"},
            {"path": "/follow_scan_due", "endpoint": self._api_follow_scan_due, "methods": ["POST"], "auth": "bear", "summary": "执行追更到点搜索"},
            {"path": "/follow_web_lookup", "endpoint": self._api_follow_web_lookup, "methods": ["POST"], "auth": "bear", "summary": "全网查询更新时间"},
            {"path": "/clear_records", "endpoint": self._api_clear_records, "methods": ["POST"], "auth": "bear", "summary": "清理处理记录"},
            {"path": "/status", "endpoint": self._api_status, "methods": ["GET"], "auth": "bear", "summary": "获取状态"},
        ]

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [
            {"cmd": "/tg115_search", "event": EventType.PluginAction, "desc": "立即搜索全部订阅", "category": "TG115自动转存", "data": {"action": "tg115_search"}},
            {"cmd": "/tg115_status", "event": EventType.PluginAction, "desc": "查看TG115状态", "category": "TG115自动转存", "data": {"action": "tg115_status"}},
        ]

    @eventmanager.register(EventType.PluginAction)
    def remote_action(self, event=None):
        if not event or not event.event_data:
            return
        action = event.event_data.get("action")
        if action not in {"tg115_search", "tg115_status"}:
            return
        channel = event.event_data.get("channel")
        userid = event.event_data.get("user")
        try:
            if action == "tg115_search":
                result = self.search_all_now(source="远程命令")
                title = "TG115自动转存：搜索完成"
                text = self._format_result(result)
            else:
                title = "TG115自动转存状态"
                text = "\n".join(f"{k}: {v}" for k, v in self._status().items())
            self.post_message(channel=channel, title=title, text=text, userid=userid)
        except Exception as err:
            logger.error("〖TG115自动转存〗远程命令失败: %s", err, exc_info=True)

    @eventmanager.register(EventType.SubscribeAdded)
    def on_subscribe_added(self, event: Event = None):
        if not event or not event.event_data or not self._auto_search_on_subscribe:
            return
        subscribe_id = int((event.event_data or {}).get("subscribe_id") or 0)
        if subscribe_id <= 0:
            return
        state = self._load_state()
        state["recent_subscribe_id"] = subscribe_id
        state["recent_subscribe_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._save_state(state)
        old_timer = self._subscribe_timers.pop(subscribe_id, None)
        if old_timer:
            old_timer.cancel()
        timer = Timer(float(self._subscribe_search_delay_seconds), self.search_subscription_by_id, kwargs={"subscribe_id": subscribe_id, "source": "新增订阅自动直搜"})
        timer.daemon = True
        self._subscribe_timers[subscribe_id] = timer
        timer.start()
        logger.info("〖TG115自动转存〗收到新增订阅事件：订阅ID=%s，%s秒后按剧名直搜TG", subscribe_id, self._subscribe_search_delay_seconds)

    def _channels(self) -> list[str]:
        from .telegram import TelegramPublicClient
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

    def _store(self) -> Tg115StateStore:
        return Tg115StateStore(Path("/config/plugins/tg115autotransfer/state.db"))

    def _target_pan_path(self) -> str:
        return cloud_path_to_pan_path(self._target_path, self._cloud_prefix)

    def _subscriptions(self) -> list[SubscriptionInfo]:
        try:
            rows = SubscribeOper().list()
        except Exception as err:
            logger.error("〖TG115自动转存〗读取订阅失败: %s", err, exc_info=True)
            return []
        active = [row for row in rows if str(getattr(row, "state", "") or "").upper() not in {"Y", "D", "DONE", "STOP"}]
        return SubscriptionMatcher.from_moviepilot(active)

    def _subscription_by_id(self, subscribe_id: int) -> Optional[SubscriptionInfo]:
        for item in self._subscriptions():
            if int(item.sid) == int(subscribe_id):
                return item
        return None

    def _searcher(self) -> TgDirectSearcher:
        return TgDirectSearcher(channels=self._channels(), timeout=self._request_timeout, proxy=self._proxy, request_interval_seconds=self._tg_search_interval_seconds, max_keywords_per_subscription=self._max_keywords_per_subscription)

    def _empty_result(self) -> Dict[str, int]:
        return {"subscriptions": 0, "requests": 0, "messages_found": 0, "links_found": 0, "matched": 0, "previewed": 0, "transferred": 0, "skipped": 0, "duplicates": 0, "existing": 0, "need_confirm": 0, "failed": 0}

    @staticmethod
    def _format_result(result: Dict[str, int]) -> str:
        return "\n".join([
            f"订阅数：{result.get('subscriptions', 0)}",
            f"TG请求：{result.get('requests', 0)}",
            f"含115消息：{result.get('messages_found', 0)}",
            f"115链接：{result.get('links_found', 0)}",
            f"匹配：{result.get('matched', 0)}",
            f"演练：{result.get('previewed', 0)}",
            f"转存：{result.get('transferred', 0)}",
            f"重复跳过：{result.get('duplicates', 0)}",
            f"已有跳过：{result.get('existing', 0)}",
            f"需确认：{result.get('need_confirm', 0)}",
            f"失败：{result.get('failed', 0)}",
        ])

    def _merge_result(self, target: Dict[str, int], part: Dict[str, int]) -> None:
        for key, value in part.items():
            target[key] = int(target.get(key, 0) or 0) + int(value or 0)

    def search_subscription(self, subscription: SubscriptionInfo, source: str = "手动直搜") -> Dict[str, int]:
        result = self._empty_result()
        run_id = self._store().start_run(source=source, subscription_id=subscription.sid, subscription_name=subscription.name, channels_count=len(self._channels()))
        matcher = SubscriptionMatcher(self._minimum_score)
        controller = TransferController(self, self._store())
        try:
            resources, search_stats = self._searcher().search_subscription(subscription)
            result["subscriptions"] = 1
            result["requests"] += search_stats.get("requests", 0)
            result["messages_found"] += search_stats.get("messages_found", 0)
            result["links_found"] += search_stats.get("links_found", 0)
            result["failed"] += search_stats.get("errors", 0)
            from .quality import evaluate_text_quality
            resources = sorted(resources, key=lambda item: evaluate_text_quality(f"{item.title}\n{item.text}", min_resolution=self._min_resolution, allow_unknown_quality=True, prefer_4k=self._prefer_4k, score_threshold=0).score, reverse=True)
            for resource in resources:
                match = matcher.match(resource, subscription)
                if not match.subscription:
                    result["skipped"] += len(resource.links)
                    continue
                part = controller.process_resource(resource, subscription, match.score)
                result["matched"] += part.get("matched", 0)
                result["previewed"] += part.get("previewed", 0)
                result["transferred"] += part.get("transferred", 0)
                result["skipped"] += part.get("skipped", 0)
                result["duplicates"] += part.get("duplicate", 0)
                result["existing"] += part.get("existing", 0)
                result["need_confirm"] += part.get("need_confirm", 0)
                result["failed"] += part.get("failed", 0)
            self._store().finish_run(run_id, messages_found=result["messages_found"], links_found=result["links_found"], matched=result["matched"], previewed=result["previewed"], transferred=result["transferred"], skipped=result["skipped"], failed=result["failed"], summary=self._format_result(result))
            if result.get("transferred", 0) > 0 and self._bridge_enabled:
                if self._bridge_delay_seconds > 0:
                    time.sleep(self._bridge_delay_seconds)
                eventmanager.send_event(EventType.PluginAction, {"action": self.EVENT_ACTION, "source": "Tg115AutoTransfer"})
            return result
        finally:
            controller.close()

    def search_subscription_by_id(self, subscribe_id: int, source: str = "指定订阅直搜") -> Dict[str, int]:
        sub = self._subscription_by_id(int(subscribe_id))
        if not sub:
            logger.warning("〖TG115自动转存〗找不到订阅ID=%s", subscribe_id)
            result = self._empty_result()
            result["failed"] = 1
            return result
        return self.search_subscription(sub, source=source)

    def search_all_now(self, source: str = "手动全部直搜") -> Dict[str, int]:
        result = self._empty_result()
        if not self._run_lock.acquire(blocking=False):
            result["skipped"] += 1
            logger.warning("〖TG115自动转存〗已有搜索任务正在运行，本次跳过")
            return result
        try:
            subs = self._subscriptions()[: self._max_subscriptions_per_run]
            for sub in subs:
                self._merge_result(result, self.search_subscription(sub, source=source))
            return result
        finally:
            self._run_lock.release()

    def search_recent_subscribe(self) -> Dict[str, int]:
        sid = int(self._load_state().get("recent_subscribe_id") or 0)
        if sid <= 0:
            result = self._empty_result()
            result["failed"] = 1
            return result
        return self.search_subscription_by_id(sid, source="最近新增订阅直搜")

    def refresh_follow_schedules_from_subscriptions(self, web_lookup: bool = False) -> Dict[str, int]:
        result = {"checked": 0, "updated": 0, "web_checked": 0, "not_matched": 0}
        if not self._follow_enabled:
            return result
        web_count = 0
        store = self._store()
        for sub in self._subscriptions():
            if not SubscriptionMatcher.is_tv(sub):
                continue
            result["checked"] += 1
            parsed = parse_follow_schedule_text(sub.keyword or sub.name, source="手动填写") if self._follow_manual_parse_enabled else None
            if (not parsed or not parsed.matched) and web_lookup and self._follow_web_lookup_enabled and web_count < self._follow_web_max_per_run:
                parsed = search_web_for_follow_schedule(sub.name, timeout=self._request_timeout, proxy=self._proxy)
                web_count += 1
                result["web_checked"] += 1
            if parsed and parsed.matched:
                store.upsert_follow_schedule({"subscribe_id": sub.sid, "title": sub.name, "parsed_days": parsed.parsed_days, "parsed_time": parsed.parsed_time, "delay_minutes": self._follow_delay_minutes, "next_run_at": calculate_next_run(parsed.parsed_days, parsed.parsed_time, self._follow_delay_minutes), "source": parsed.source, "confidence": parsed.confidence, "enabled": True, "raw_text": parsed.raw_text, "episode_count": parsed.episode_count, "last_web_check_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S") if parsed.source == "全网查询" else ""})
                result["updated"] += 1
            else:
                result["not_matched"] += 1
        return result

    def follow_scan_due(self) -> Dict[str, int]:
        result = self._empty_result()
        if not self._follow_enabled:
            return result
        store = self._store()
        for item in store.due_follow_schedules(limit=20):
            sid = int(item.get("subscribe_id") or 0)
            self._merge_result(result, self.search_subscription_by_id(sid, source=f"追更直搜：{item.get('title')}"))
            next_run = calculate_next_run(str(item.get("parsed_days") or "daily"), str(item.get("parsed_time") or ""), int(item.get("delay_minutes") or self._follow_delay_minutes))
            store.update_follow_schedule(sid, next_run_at=next_run, last_run_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        return result

    def _status(self) -> dict:
        state = self._load_state()
        stats = self._store().stats()
        controller = TransferController(self, self._store())
        cooldown_until = controller.cooldown_until()
        return {"version": self.plugin_version, "enabled": self._enabled, "dry_run": self._dry_run, "channels": len(self._channels()), "target_pan_path": self._target_pan_path(), "scheduled_search_enabled": self._scheduled_search_enabled, "auto_search_on_subscribe": self._auto_search_on_subscribe, "follow_enabled": self._follow_enabled, "p115_cooldown_until": cooldown_until.strftime("%Y-%m-%d %H:%M:%S") if cooldown_until else "-", "records": stats.get("records", 0), "today_runs": stats.get("today_runs", 0), "follow_count": stats.get("follow_count", 0), "next_follow": stats.get("next_follow"), "recent_subscribe_id": state.get("recent_subscribe_id", "-")}

    async def _api_search_all_now(self) -> dict:
        return {"code": 0, "message": "搜索完成", "data": self.search_all_now(source="手动全部直搜")}

    async def _api_search_recent_subscribe(self) -> dict:
        return {"code": 0, "message": "最近新增订阅搜索完成", "data": self.search_recent_subscribe()}

    async def _api_search_subscription(self, subscribe_id: int) -> dict:
        return {"code": 0, "message": "指定订阅搜索完成", "data": self.search_subscription_by_id(int(subscribe_id), source="手动指定订阅直搜")}

    async def _api_follow_scan_due(self) -> dict:
        return {"code": 0, "message": "追更到点搜索完成", "data": self.follow_scan_due()}

    async def _api_follow_web_lookup(self) -> dict:
        return {"code": 0, "message": "更新时间查询完成", "data": self.refresh_follow_schedules_from_subscriptions(web_lookup=True)}

    async def _api_clear_records(self) -> dict:
        self._store().clear_records()
        return {"code": 0, "message": "处理记录已清理", "data": None}

    async def _api_status(self) -> dict:
        return {"code": 0, "message": "success", "data": self._status()}

    def stop_service(self):
        for timer in list(self._subscribe_timers.values()):
            try:
                timer.cancel()
            except Exception:
                pass
        self._subscribe_timers.clear()

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [{"component": "VForm", "content": [
            {"component": "VAlert", "props": {"type": "success", "variant": "tonal", "text": "v0.6.0：新增资源质量检测，默认优先4K/2160p；真实转存前默认只跳过BDMV蓝光目录结构，其他结构放行；可填写自定义跳过结构/关键词。v0.5.0 全新重构：插件按MoviePilot订阅名直接搜索TG公开频道，命中115分享后即时判断、去重、限速转存。只保存必要处理记录用于防重复和失败重试。"}},
            {"component": "VAlert", "props": {"type": "warning", "variant": "tonal", "text": "建议首次使用保持“仅日志演练”开启。确认搜索和匹配结果正确后，再关闭演练进行真实转存。"}},
            {"component": "VDivider", "props": {"class": "my-3"}},
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": "一、基础设置"}},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件", "hint": "关闭后不会运行定时任务和自动搜索。", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "dry_run", "label": "仅日志演练", "hint": "默认开启，只记录会处理什么，不真实转存。", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "auto_create_dir", "label": "自动创建115目录", "hint": "目标路径不存在时尝试创建。", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "request_timeout", "label": "请求超时秒数", "type": "number", "hint": "TG/115请求超过多久算失败。", "persistent-hint": True}}]},
            ]},
            {"component": "VTextarea", "props": {"model": "channels", "label": "公开TG频道", "rows": 5, "placeholder": "每行一个，例如：\nQukanMovie\nhttps://t.me/gimy115\n@gimy115", "hint": "插件会用 t.me/s/频道?q=订阅名 直接搜索。", "persistent-hint": True}},
            {"component": "VTextField", "props": {"model": "cookies", "label": "115 Cookie", "type": "password", "placeholder": "UID=...; CID=...; SEID=...; KID=...", "hint": "真实转存必须填写；演练模式可以不填。", "persistent-hint": True}},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": "cloud_prefix", "label": "CloudDrive2 前缀", "placeholder": "/115open", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": "target_path", "label": "转存目标路径", "placeholder": "/115open/最近接收/TG", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": "proxy", "label": "TG代理（可选）", "placeholder": "http://127.0.0.1:7890", "persistent-hint": True}}]},
            ]},
            {"component": "VDivider", "props": {"class": "my-3"}},
            {"component": "VAlert", "props": {"type": "success", "variant": "tonal", "text": "二、TG直接搜索设置"}},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "auto_search_on_subscribe", "label": "新增订阅后自动搜索", "hint": "默认开启。新增MP订阅后等待30秒按剧名搜索TG。", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": "subscribe_search_delay_seconds", "label": "新增订阅后等待秒数", "type": "number", "hint": "默认30秒。", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "scheduled_search_enabled", "label": "定时搜索全部订阅", "hint": "默认关闭。订阅很多时不建议频繁全量搜索。", "persistent-hint": True}}]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VCronField", "props": {"model": "cron", "label": "自动搜索周期", "placeholder": "点击选择时间", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "max_subscriptions_per_run", "label": "每轮最多搜索订阅数", "type": "number", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "max_keywords_per_subscription", "label": "每订阅最多关键词数", "type": "number", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "tg_search_interval_seconds", "label": "TG搜索间隔秒数", "type": "number", "persistent-hint": True}}]},
            ]},
            {"component": "VDivider", "props": {"class": "my-3"}},
            {"component": "VAlert", "props": {"type": "warning", "variant": "tonal", "text": "三、去重、匹配和115保护"}},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "minimum_score", "label": "最低匹配分", "type": "number", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "retry_limit", "label": "失败重试次数", "type": "number", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "max_transfers_per_run", "label": "单轮最多转存", "type": "number", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "max_transfers_per_subscription", "label": "每订阅最多转存", "type": "number", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "transfer_delay_seconds", "label": "两次转存间隔秒数", "type": "number", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "p115_cooldown_minutes", "label": "限流后冷却分钟", "type": "number", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "stop_on_rate_limit", "label": "限流后立刻停止", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "bridge_enabled", "label": "通知整理桥接", "persistent-hint": True}}]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "tv_only_missing_episodes", "label": "剧集只补缺集", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "auto_transfer_unknown_episode", "label": "识别不出集数也转", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "skip_low_quality", "label": "跳过枪版低质", "persistent-hint": True}}]},
            ]},
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": "四、资源质量检测：开启后优先4K/2160p；默认只跳过BDMV蓝光目录结构。VIDEO_TS、ISO、原盘、多视频、合集、整季包默认放行；需要额外跳过时，在自定义关键词中按行填写。演练模式不读取115分享结构。"}},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "quality_filter_enabled", "label": "启用资源质量检测", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "prefer_4k", "label": "优先4K/2160p", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "min_resolution", "label": "最低分辨率", "placeholder": "1080p", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "skip_bdmv_structure", "label": "跳过BDMV结构", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "max_quality_probe_per_subscription", "label": "每订阅最多预检候选", "type": "number", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "allow_unknown_quality", "label": "允许未知质量转存", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "quality_score_threshold", "label": "质量分阈值", "type": "number", "persistent-hint": True}}]},
            ]},
            {"component": "VTextarea", "props": {"model": "custom_skip_structure_keywords", "label": "自定义跳过结构/关键词", "rows": 3, "placeholder": "每行一个；留空则除BDMV外其他结构默认放行。\n例如：VIDEO_TS\n.iso\n原盘\nsample", "persistent-hint": True}},
            {"component": "VDivider", "props": {"class": "my-3"}},
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": "五、电视剧追更"}},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "follow_enabled", "label": "启用电视剧追更搜索", "hint": "默认关闭。", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "follow_manual_parse_enabled", "label": "识别手动填写更新时间", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "follow_web_lookup_enabled", "label": "自动上网查询更新时间", "hint": "默认关闭。", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": "follow_delay_minutes", "label": "更新时间后延迟搜索分钟", "type": "number", "hint": "默认35分钟。", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": "follow_max_transfers_per_run", "label": "单剧每次最多转存", "type": "number", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": "follow_daily_trigger_limit", "label": "每部剧每天最多触发", "type": "number", "persistent-hint": True}}]},
            ]},
            {"component": "VDivider", "props": {"class": "my-3"}},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "notify_enabled", "label": "发MP通知", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "notify_scan_summary", "label": "发运行总结", "persistent-hint": True}}]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "notify_empty_scan", "label": "没结果也通知", "persistent-hint": True}}]},
            ]},
        ]}], self._config_dict()

    def get_page(self) -> Optional[List[dict]]:
        status = self._status()
        stats = self._store().stats()
        by_status = stats.get("by_status") or {}
        next_follow = stats.get("next_follow") or {}
        status_text = f"版本：{self.plugin_version} ｜ 启用：{'是' if self._enabled else '否'} ｜ 演练：{'是' if self._dry_run else '否'} ｜ TG频道：{len(self._channels())} ｜ 目标目录：{self._target_pan_path()} ｜ 115冷却：{status.get('p115_cooldown_until')}"
        stats_text = f"处理记录：{stats.get('records', 0)} ｜ 今日搜索次数：{stats.get('today_runs', 0)} ｜ 已转存：{by_status.get('transferred', 0)} ｜ 演练：{by_status.get('previewed', 0)} ｜ 重复跳过：{by_status.get('skipped_duplicate', 0)} ｜ 失败：{by_status.get('failed', 0)} ｜ 追更剧：{stats.get('follow_count', 0)} ｜ 下次追更：{next_follow.get('title', '-') if next_follow else '-'} {next_follow.get('next_run_at', '') if next_follow else ''}"
        return [
            {"component": "VAlert", "props": {"type": "success", "variant": "tonal", "text": "v0.6.0：资源质量检测版。默认优先4K/2160p，真实转存前默认只跳过BDMV蓝光目录结构；其他结构默认放行，可用自定义关键词额外跳过。v0.5.0：全新直搜版。插件根据MP订阅名直接搜索TG公开频道：t.me/s/频道?q=订阅名。只保存必要处理记录，用于防重复、失败重试和115限速。"}},
            {"component": "VAlert", "props": {"type": "warning", "variant": "tonal", "text": "操作区每张卡片都写明用途。真实转存前建议先保持“仅日志演练”开启。"}},
            {"component": "VRow", "props": {"class": "my-2"}, "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VCard", "props": {"variant": "outlined", "class": "h-100"}, "content": [
                    {"component": "VCardTitle", "text": "立即搜索全部订阅"},
                    {"component": "VCardText", "text": "对当前所有活跃订阅逐个搜索TG频道。适合刚配置好插件后测试。演练模式下只记录命中结果，不真实转存。"},
                    {"component": "VCardActions", "content": [{"component": "VBtn", "props": {"color": "primary", "prepend-icon": "mdi-magnify", "text": "立即搜索全部订阅"}, "events": {"click": {"api": "plugin/Tg115AutoTransfer/search_all_now", "method": "post"}}}]},
                ]}]},
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VCard", "props": {"variant": "outlined", "class": "h-100"}, "content": [
                    {"component": "VCardTitle", "text": "搜索最近新增订阅"},
                    {"component": "VCardText", "text": "只搜索最近一次新增的MP订阅。新增订阅后插件也会默认等待30秒自动搜索。"},
                    {"component": "VCardActions", "content": [{"component": "VBtn", "props": {"color": "success", "variant": "tonal", "prepend-icon": "mdi-playlist-search", "text": "搜索最近新增订阅"}, "events": {"click": {"api": "plugin/Tg115AutoTransfer/search_recent_subscribe", "method": "post"}}}]},
                ]}]},
            ]},
            {"component": "VRow", "props": {"class": "my-2"}, "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VCard", "props": {"variant": "outlined", "class": "h-100"}, "content": [
                    {"component": "VCardTitle", "text": "执行到点追更搜索"},
                    {"component": "VCardText", "text": "只搜索已经到更新时间的电视剧。搜索方式同样是按剧名直接搜索TG频道。"},
                    {"component": "VCardActions", "content": [{"component": "VBtn", "props": {"color": "secondary", "variant": "tonal", "prepend-icon": "mdi-calendar-search", "text": "执行到点追更搜索"}, "events": {"click": {"api": "plugin/Tg115AutoTransfer/follow_scan_due", "method": "post"}}}]},
                ]}]},
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VCard", "props": {"variant": "outlined", "class": "h-100"}, "content": [
                    {"component": "VCardTitle", "text": "全网查询更新时间"},
                    {"component": "VCardText", "text": "从公开网页查询电视剧更新时间。默认关闭，需要在配置里打开；结果可能不准，只作追更参考。"},
                    {"component": "VCardActions", "content": [{"component": "VBtn", "props": {"color": "secondary", "variant": "outlined", "prepend-icon": "mdi-web", "text": "全网查询更新时间"}, "events": {"click": {"api": "plugin/Tg115AutoTransfer/follow_web_lookup", "method": "post"}}}]},
                ]}]},
            ]},
            {"component": "VCard", "props": {"variant": "outlined", "class": "mb-2"}, "content": [
                {"component": "VCardTitle", "text": "清理处理记录"},
                {"component": "VCardText", "text": "清空已处理链接和搜索运行摘要。清空后同一资源可能再次被处理，慎用。"},
                {"component": "VCardActions", "content": [{"component": "VBtn", "props": {"color": "error", "variant": "outlined", "prepend-icon": "mdi-delete-alert", "text": "清理处理记录"}, "events": {"click": {"api": "plugin/Tg115AutoTransfer/clear_records", "method": "post"}}}]},
            ]},
            {"component": "VCard", "props": {"variant": "outlined", "class": "mb-2"}, "content": [{"component": "VCardTitle", "text": "当前状态"}, {"component": "VCardText", "text": status_text}]},
            {"component": "VCard", "props": {"variant": "outlined"}, "content": [{"component": "VCardTitle", "text": "处理统计"}, {"component": "VCardText", "text": stats_text}]},
        ]
