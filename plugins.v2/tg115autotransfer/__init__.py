from __future__ import annotations

from datetime import datetime
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger

from app.core.event import eventmanager
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType

from .matcher import SubscriptionMatcher
from .models import TelegramResource
from .p115 import P115TransferClient
from .telegram import TelegramPublicClient
from .text import cloud_path_to_pan_path, normalize_posix_path


class Tg115AutoTransfer(_PluginBase):
    plugin_name = "TG 115自动转存"
    plugin_desc = "增量扫描公开TG频道，匹配MoviePilot订阅并独立转存115资源"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/cloud.png"
    plugin_version = "0.1.0"
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
    _max_transfers_per_run = 10
    _request_timeout = 20
    _proxy = ""
    _bridge_enabled = True
    _dry_run = False
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
        self._max_transfers_per_run = max(1, int(config.get("max_transfers_per_run") or 10))
        self._request_timeout = max(5, int(config.get("request_timeout") or 20))
        self._proxy = str(config.get("proxy") or "").strip()
        self._bridge_enabled = bool(config.get("bridge_enabled", True))
        self._dry_run = bool(config.get("dry_run", False))
        self.update_config({
            "enabled": self._enabled,
            "cron": self._cron,
            "channels": self._channels_text,
            "cookies": self._cookies,
            "cloud_prefix": self._cloud_prefix,
            "target_path": self._target_path,
            "auto_create_dir": self._auto_create_dir,
            "first_run_backfill": self._first_run_backfill,
            "minimum_score": self._minimum_score,
            "max_transfers_per_run": self._max_transfers_per_run,
            "request_timeout": self._request_timeout,
            "proxy": self._proxy,
            "bridge_enabled": self._bridge_enabled,
            "dry_run": self._dry_run,
        })
        logger.info("〖TG115自动转存〗初始化完成 enabled=%s channels=%s", self._enabled, len(self._channels()))

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
            "id": "Tg115AutoTransfer_scan",
            "name": "TG公开频道增量扫描与115转存",
            "trigger": trigger,
            "func": self.scan_once,
            "kwargs": {},
        }]

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [
            {"cmd": "/tg115_scan", "event": EventType.PluginAction, "desc": "立即扫描TG频道", "category": "TG115自动转存", "data": {"action": "tg115_scan"}},
            {"cmd": "/tg115_status", "event": EventType.PluginAction, "desc": "查看TG115状态", "category": "TG115自动转存", "data": {"action": "tg115_status"}},
            {"cmd": "/tg115_reset", "event": EventType.PluginAction, "desc": "重置TG频道游标", "category": "TG115自动转存", "data": {"action": "tg115_reset"}},
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {"path": "/scan_now", "endpoint": self._api_scan_now, "methods": ["POST"], "summary": "立即扫描TG频道"},
            {"path": "/status", "endpoint": self._api_status, "methods": ["GET"], "summary": "获取运行状态"},
            {"path": "/reset", "endpoint": self._api_reset, "methods": ["POST"], "summary": "重置频道游标与去重记录"},
        ]

    @eventmanager.register(EventType.PluginAction)
    def remote_action(self, event=None):
        if not event or not event.event_data:
            return
        action = event.event_data.get("action")
        if action not in {"tg115_scan", "tg115_status", "tg115_reset"}:
            return
        channel = event.event_data.get("channel")
        userid = event.event_data.get("user")
        try:
            if action == "tg115_scan":
                result = self.scan_once(source="远程命令")
                title = "TG115自动转存：扫描完成"
                text = f"新消息 {result['new_messages']}，匹配 {result['matched']}，转存 {result['transferred']}，演练 {result['previewed']}，失败 {result['errors']}"
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

    def _subscriptions(self):
        try:
            rows = SubscribeOper().list()
        except Exception as err:
            logger.error("〖TG115自动转存〗读取订阅失败: %s", err, exc_info=True)
            return []
        # N/空状态通常是进行中；过滤明确完成/停用项。
        active = [row for row in rows if str(getattr(row, "state", "") or "").upper() not in {"Y", "D", "DONE", "STOP"}]
        return SubscriptionMatcher.from_moviepilot(active)

    def _target_pan_path(self) -> str:
        return cloud_path_to_pan_path(self._target_path, self._cloud_prefix)

    def scan_once(self, source: str = "定时任务") -> Dict[str, int]:
        result = {"new_messages": 0, "matched": 0, "transferred": 0, "previewed": 0, "skipped": 0, "errors": 0}
        if not self._run_lock.acquire(blocking=False):
            result["skipped"] += 1
            return result
        tg_client = None
        try:
            channels = self._channels()
            if not channels:
                logger.warning("〖TG115自动转存〗未配置公开频道")
                return result
            subscriptions = self._subscriptions()
            if not subscriptions:
                logger.warning("〖TG115自动转存〗没有可匹配的活动订阅")
                return result

            state = self._load_state()
            cursors = state.setdefault("cursors", {})
            initialized = state.setdefault("initialized_channels", {})
            hashes = state.setdefault("hashes", {})
            processed = state.setdefault("processed_shares", {})
            stats = state.setdefault("stats", {})
            stats["runs"] = int(stats.get("runs", 0)) + 1
            stats["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            matcher = SubscriptionMatcher(self._minimum_score)
            tg_client = TelegramPublicClient(timeout=self._request_timeout, proxy=self._proxy)
            transfer_client = None
            successful_transfer = False

            for channel in channels:
                try:
                    messages = sorted(tg_client.fetch_latest(channel), key=lambda item: item.message_id)
                except Exception as err:
                    logger.error("〖TG115自动转存〗频道 %s 读取失败: %s", channel, err)
                    result["errors"] += 1
                    continue
                if not messages:
                    # 即便当前页没有115资源，也标记频道已初始化，避免未来第一条资源被误当历史跳过。
                    initialized[channel] = True
                    continue
                last_id = int(cursors.get(channel, 0) or 0)
                if not initialized.get(channel) and not self._first_run_backfill:
                    initialized[channel] = True
                    cursors[channel] = max(item.message_id for item in messages)
                    for item in messages:
                        hashes[f"{channel}:{item.message_id}"] = item.content_hash
                    logger.info("〖TG115自动转存〗频道 %s 首次建立游标，不回补历史", channel)
                    continue
                initialized[channel] = True

                for resource in messages:
                    message_key = f"{channel}:{resource.message_id}"
                    changed = hashes.get(message_key) != resource.content_hash
                    if resource.message_id <= last_id and not changed:
                        continue
                    result["new_messages"] += 1
                    match = matcher.match(resource, subscriptions)
                    if not match.subscription or match.score < self._minimum_score:
                        hashes[message_key] = resource.content_hash
                        result["skipped"] += 1
                        continue
                    result["matched"] += 1
                    logger.info("〖TG115自动转存〗匹配：%s -> %s，分数=%s", resource.title, match.subscription.name, match.score)

                    message_complete = True
                    for share in resource.links:
                        completed_count = result["transferred"] + result["previewed"]
                        if completed_count >= self._max_transfers_per_run:
                            message_complete = False
                            break
                        # 同一分享链接在消息被编辑或重新发布后允许再次处理，支持频道更新同一资源。
                        processed_key = f"{share.key}|{resource.content_hash}"
                        if processed_key in processed:
                            result["skipped"] += 1
                            continue
                        if self._dry_run:
                            logger.info("〖TG115自动转存〗演练：将转存 %s 到 %s", share.url, self._target_pan_path())
                            result["previewed"] += 1
                            message_complete = False
                            continue
                        try:
                            if transfer_client is None:
                                transfer_client = P115TransferClient(self._cookies, auto_create=self._auto_create_dir)
                            transfer = transfer_client.transfer(share, self._target_pan_path())
                            if not transfer.success:
                                raise RuntimeError(transfer.message)
                            processed[processed_key] = {
                                "share_key": share.key,
                                "content_hash": resource.content_hash,
                                "time": stats["last_run"],
                                "channel": channel,
                                "message_id": resource.message_id,
                                "subscription_id": match.subscription.sid,
                                "subscription": match.subscription.name,
                                "target_cid": transfer.target_cid,
                            }
                            result["transferred"] += 1
                            successful_transfer = True
                        except Exception as err:
                            message_complete = False
                            logger.error("〖TG115自动转存〗转存失败 %s: %s", share.url, err, exc_info=True)
                            result["errors"] += 1
                    # 失败、演练或因单轮上限未处理完的消息不写入哈希，下一轮继续处理。
                    if message_complete:
                        hashes[message_key] = resource.content_hash
                cursors[channel] = max(int(cursors.get(channel, 0) or 0), max(item.message_id for item in messages))

            # 限制状态体积。
            if len(processed) > 2000:
                ordered = list(processed.items())[-2000:]
                state["processed_shares"] = dict(ordered)
            if len(hashes) > 3000:
                state["hashes"] = dict(list(hashes.items())[-3000:])
            stats["transferred"] = int(stats.get("transferred", 0)) + result["transferred"]
            stats["previewed"] = int(stats.get("previewed", 0)) + result["previewed"]
            stats["errors"] = int(stats.get("errors", 0)) + result["errors"]
            self._save_state(state)

            if successful_transfer and self._bridge_enabled:
                eventmanager.send_event(EventType.PluginAction, {"action": self.EVENT_ACTION, "source": "Tg115AutoTransfer"})
                logger.info("〖TG115自动转存〗已通知115整理入队桥接")
            return result
        finally:
            if tg_client:
                tg_client.close()
            self._run_lock.release()

    def _status(self) -> dict:
        state = self._load_state()
        stats = state.get("stats") or {}
        return {
            "enabled": self._enabled,
            "version": self.plugin_version,
            "channels": len(self._channels()),
            "cron": self._cron,
            "target_cloud_path": self._target_path,
            "target_pan_path": self._target_pan_path(),
            "minimum_score": self._minimum_score,
            "bridge_enabled": self._bridge_enabled,
            "processed_shares": len(state.get("processed_shares") or {}),
            "last_run": stats.get("last_run") or "-",
            "transferred": stats.get("transferred", 0),
            "previewed": stats.get("previewed", 0),
            "errors": stats.get("errors", 0),
        }

    async def _api_scan_now(self) -> dict:
        try:
            return {"code": 0, "message": "扫描完成", "data": self.scan_once(source="API")}
        except Exception as err:
            return {"code": 1, "message": str(err), "data": None}

    async def _api_status(self) -> dict:
        return {"code": 0, "message": "success", "data": self._status()}

    async def _api_reset(self) -> dict:
        self._save_state({})
        return {"code": 0, "message": "状态已重置", "data": None}

    def stop_service(self):
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [{
            "component": "VForm",
            "content": [
                {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": "v0.1.0：扫描公开TG频道的新消息，匹配MP订阅后直接转存115；转存成功仅向桥接插件发送专用成功事件。"}},
                {"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "dry_run", "label": "仅日志演练"}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "bridge_enabled", "label": "联动整理桥接"}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "first_run_backfill", "label": "首次回补当前页"}}]},
                ]},
                {"component": "VTextarea", "props": {"model": "channels", "label": "公开TG频道（每行一个）", "rows": 5, "placeholder": "channel_name\nhttps://t.me/channel_name\n@channel_name"}},
                {"component": "VTextField", "props": {"model": "cookies", "label": "115 Cookie", "type": "password", "placeholder": "UID=...; CID=...; SEID=...; KID=..."}},
                {"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextField", "props": {"model": "cloud_prefix", "label": "CloudDrive2 前缀", "placeholder": "/115open"}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextField", "props": {"model": "target_path", "label": "转存目标完整路径", "placeholder": "/115open/最近接收/TG"}}]},
                ]},
                {"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "auto_create_dir", "label": "自动创建115目录"}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "minimum_score", "label": "自动转存最低分", "type": "number"}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "max_transfers_per_run", "label": "单轮最多转存", "type": "number"}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "request_timeout", "label": "请求超时（秒）", "type": "number"}}]},
                ]},
                {"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextField", "props": {"model": "cron", "label": "扫描 Cron", "placeholder": "*/15 * * * *"}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextField", "props": {"model": "proxy", "label": "TG代理（可选）", "placeholder": "http://127.0.0.1:7890"}}]},
                ]},
                {"component": "VAlert", "props": {"type": "warning", "variant": "tonal", "text": "目标路径需包含 CloudDrive2 前缀。例：前缀 /115open，目标 /115open/最近接收/TG，实际115接收目录会自动换算为 /最近接收/TG。"}},
            ],
        }], {
            "enabled": False,
            "cron": "*/15 * * * *",
            "channels": "",
            "cookies": "",
            "cloud_prefix": "/115open",
            "target_path": "/115open/最近接收/TG",
            "auto_create_dir": True,
            "first_run_backfill": False,
            "minimum_score": 80,
            "max_transfers_per_run": 10,
            "request_timeout": 20,
            "proxy": "",
            "bridge_enabled": True,
            "dry_run": False,
        }

    def get_page(self) -> Optional[List[dict]]:
        status = self._status()
        return [
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": "TG115自动转存 v0.1.0：公开频道增量扫描、订阅匹配、独立115转存、专用桥接事件。"}},
            {"component": "VCard", "props": {"variant": "outlined"}, "content": [
                {"component": "VCardTitle", "text": "运行状态"},
                {"component": "VCardText", "text": " ｜ ".join(f"{key}: {value}" for key, value in status.items())},
            ]},
        ]
