from datetime import datetime
from pathlib import Path
from threading import Lock, Thread
from time import sleep, time
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import inspect, text

from app.chain.storage import StorageChain
from app.core.event import Event, eventmanager
from app.chain.transfer import TransferChain
from app.db import SessionFactory
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import FileItem
from app.schemas.types import EventType

from app.plugins.p115strmhelper.utils.storage_item import (
    resolve_directory_via_parent_list,
    resolve_file_via_parent_list,
)


class P115TransferEnqueueBridge(_PluginBase):
    """
    115 下载历史整理桥接插件

    轮询 DownloadHistory 中指定来源用户的新记录，按 path 去重后直接调用 MoviePilot 原生 TransferChain.do_transfer。
    可选包装 P115StrmHelper 分享转存成功回调；成功后直接把配置目录加入原生整理队列。
    支持手动立即运行、定时补漏、中文状态、可读时间和运行统计。
    """

    plugin_name = "115整理入队桥接"
    plugin_desc = "轮询115下载历史，分享转存成功后自动入队配置目录"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/cloud.png"
    plugin_version = "0.3.0"
    plugin_author = "outxool"
    author_url = "https://github.com/outxool"
    plugin_config_prefix = "p115transferenqueuebridge_"
    plugin_order = 1
    auth_level = 1

    DEFAULT_SOURCE_USERNAME = "P115StrgmSub"
    DEFAULT_INTERVAL = 120
    DEFAULT_DEBOUNCE_SECONDS = 300
    DEFAULT_HISTORY_LIMIT = 500
    DEFAULT_SHARE_TRANSFER_DELAY = 30
    DEFAULT_SHARE_TRANSFER_ENQUEUE_ROOTS = ["/最近接收"]
    DEFAULT_RECENT_EVENTS_LIMIT = 50
    RUNTIME_STATE_KEY = "runtime_state"
    RECENT_EVENTS_KEY = "recent_events"
    STATUS_TEXT_MAP = {
        "ENQUEUE": "✅ 已入队",
        "SHARE-ENQUEUE": "✅ 已入队",
        "DRYRUN": "🧪 演练",
        "DONE": "✅ 已完成",
        "SHARE-DONE": "✅ 已完成",
        "SKIP": "⚠️ 已跳过",
        "SHARE-SKIP": "⚠️ 已跳过",
        "ERROR": "❌ 错误",
        "SHARE-ERROR": "❌ 错误",
        "INFO": "ℹ️ 信息",
        "CURSOR": "🔰 游标",
        "MANUAL": "🖱️ 手动",
        "CACHE": "🧹 缓存",
        "SCHEDULE": "⏰ 定时",
    }

    _enabled: bool = False
    _cron: str = ""
    _interval: int = DEFAULT_INTERVAL
    _source_username: str = DEFAULT_SOURCE_USERNAME
    _debounce_seconds: int = DEFAULT_DEBOUNCE_SECONDS
    _allowed_roots_text: str = ""
    _allowed_roots: List[Path] = []
    _dry_run: bool = False
    _clouddrive2_enabled: bool = True
    _clouddrive2_prefix: str = "/115open"
    _share_transfer_hook_enabled: bool = False
    _share_transfer_delay: int = DEFAULT_SHARE_TRANSFER_DELAY
    _share_transfer_enqueue_roots_text: str = ""
    _share_transfer_enqueue_roots: List[Path] = []
    _share_roots_schedule_enabled: bool = False
    _share_roots_schedule_cron: str = ""
    _recent_events_limit: int = DEFAULT_RECENT_EVENTS_LIMIT
    _share_transfer_hook_lock = Lock()
    _runtime_lock = Lock()
    _share_transfer_hooked_helper: Any = None
    _transferchain: Optional[TransferChain] = None
    _storagechain: Optional[StorageChain] = None

    def init_plugin(self, config: dict = None):
        """
        初始化插件
        """
        self._transferchain = TransferChain()
        self._storagechain = StorageChain()

        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._cron = str(config.get("cron") or "").strip()
        self._interval = self._safe_int(config.get("interval"), self.DEFAULT_INTERVAL)
        self._source_username = (
            str(config.get("source_username") or self.DEFAULT_SOURCE_USERNAME).strip()
            or self.DEFAULT_SOURCE_USERNAME
        )
        self._debounce_seconds = self._safe_int(
            config.get("debounce_seconds"),
            self.DEFAULT_DEBOUNCE_SECONDS,
        )
        self._allowed_roots_text = str(config.get("allowed_roots") or "").strip()
        self._allowed_roots = self._parse_allowed_roots(self._allowed_roots_text)
        self._dry_run = bool(config.get("dry_run", False))
        self._clouddrive2_enabled = bool(config.get("clouddrive2_enabled", True))
        self._clouddrive2_prefix = str(config.get("clouddrive2_prefix") or "/115open").strip() or "/115open"
        self._share_transfer_hook_enabled = bool(config.get("share_transfer_hook_enabled", False))
        self._share_transfer_delay = max(
            self._safe_int(config.get("share_transfer_delay"), self.DEFAULT_SHARE_TRANSFER_DELAY),
            0,
        )
        self._share_transfer_enqueue_roots_text = str(
            config.get("share_transfer_enqueue_roots")
            or config.get("share_transfer_fallback_roots")
            or "\n".join(self.DEFAULT_SHARE_TRANSFER_ENQUEUE_ROOTS)
        ).strip()
        self._share_transfer_enqueue_roots = self._parse_allowed_roots(self._share_transfer_enqueue_roots_text)
        self._share_roots_schedule_enabled = bool(config.get("share_roots_schedule_enabled", False))
        self._share_roots_schedule_cron = str(config.get("share_roots_schedule_cron") or "").strip()
        self._recent_events_limit = max(
            self._safe_int(config.get("recent_events_limit"), self.DEFAULT_RECENT_EVENTS_LIMIT),
            10,
        )

        self.update_config(
            {
                "enabled": self._enabled,
                "cron": self._cron,
                "interval": self._interval,
                "source_username": self._source_username,
                "debounce_seconds": self._debounce_seconds,
                "allowed_roots": self._allowed_roots_text,
                "dry_run": self._dry_run,
                "clouddrive2_enabled": self._clouddrive2_enabled,
                "clouddrive2_prefix": self._clouddrive2_prefix,
                "share_transfer_hook_enabled": self._share_transfer_hook_enabled,
                "share_transfer_delay": self._share_transfer_delay,
                "share_transfer_enqueue_roots": self._share_transfer_enqueue_roots_text,
                "share_roots_schedule_enabled": self._share_roots_schedule_enabled,
                "share_roots_schedule_cron": self._share_roots_schedule_cron,
                "recent_events_limit": self._recent_events_limit,
            }
        )

        self._ensure_share_transfer_hook()

        logger.info(
            "【115整理桥接】插件初始化完成 enabled=%s cron=%s interval=%s source_username=%s dry_run=%s share_transfer_hook=%s",
            self._enabled,
            self._cron or "<interval>",
            self._interval,
            self._source_username,
            self._dry_run,
            self._share_transfer_hook_enabled,
        )

    def get_state(self) -> bool:
        """
        获取插件状态
        """
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        """
        return [
            {
                "cmd": "/115bridge_poll",
                "event": EventType.PluginAction,
                "desc": "立即运行115下载历史整理桥接",
                "category": "115整理桥接",
                "data": {"action": "p115bridge_poll"},
            },
            {
                "cmd": "/115bridge_share",
                "event": EventType.PluginAction,
                "desc": "立即入队115分享转存目录",
                "category": "115整理桥接",
                "data": {"action": "p115bridge_share"},
            },
            {
                "cmd": "/115bridge_status",
                "event": EventType.PluginAction,
                "desc": "查看115整理桥接状态",
                "category": "115整理桥接",
                "data": {"action": "p115bridge_status"},
            },
            {
                "cmd": "/115bridge_clear_cache",
                "event": EventType.PluginAction,
                "desc": "清理115整理桥接去重缓存",
                "category": "115整理桥接",
                "data": {"action": "p115bridge_clear_cache"},
            },
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件 API
        """
        return [
            {
                "path": "/poll_now",
                "endpoint": self._api_poll_now,
                "methods": ["POST"],
                "summary": "立即运行一次下载历史轮询",
            },
            {
                "path": "/enqueue_share_roots",
                "endpoint": self._api_enqueue_share_roots,
                "methods": ["POST"],
                "summary": "立即将分享转存目录加入整理队列",
            },
            {
                "path": "/reset_cursor",
                "endpoint": self._api_reset_cursor,
                "methods": ["POST"],
                "summary": "重置下载历史游标",
            },
            {
                "path": "/clear_cache",
                "endpoint": self._api_clear_cache,
                "methods": ["POST"],
                "summary": "清理路径去重缓存",
            },
            {
                "path": "/status",
                "endpoint": self._api_status,
                "methods": ["GET"],
                "summary": "获取插件状态",
            },
        ]

    def get_service(self) -> List[Dict[str, Any]] | None:
        """
        注册插件公共服务
        """
        if not self._enabled:
            return None

        self._ensure_share_transfer_hook()
        services: List[Dict[str, Any]] = []

        trigger = self._build_trigger()
        if trigger:
            services.append(
                {
                    "id": "P115TransferEnqueueBridge_poll",
                    "name": "115下载历史整理入队桥接",
                    "trigger": trigger,
                    "func": self.poll_download_history,
                    "kwargs": {},
                }
            )

        if self._share_roots_schedule_enabled and self._share_roots_schedule_cron:
            try:
                services.append(
                    {
                        "id": "P115TransferEnqueueBridge_share_roots_schedule",
                        "name": "115分享目录定时补漏入队",
                        "trigger": CronTrigger.from_crontab(self._share_roots_schedule_cron),
                        "func": self.enqueue_share_roots_scheduled,
                        "kwargs": {},
                    }
                )
            except Exception as err:
                logger.error(f"【115整理桥接】分享目录定时补漏 Cron 无效: {err}", exc_info=True)
                self._record_recent_event("ERROR", "-", f"分享目录定时补漏 Cron 无效: {err}")

        return services or None

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面
        """
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "density": "compact",
                            "class": "mb-2",
                            "text": "v0.3.0：分享转存成功后直接将配置目录加入 MP 原生整理队列；支持立即运行、定时补漏、中文状态和可读时间。",
                        },
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{"component": "VSwitch", "props": {"model": "dry_run", "label": "仅日志演练"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{"component": "VTextField", "props": {"model": "interval", "label": "轮询间隔（秒）", "type": "number"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{"component": "VTextField", "props": {"model": "debounce_seconds", "label": "去重冷却（秒）", "type": "number"}}],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{"component": "VTextField", "props": {"model": "source_username", "label": "来源用户名", "placeholder": "P115StrgmSub"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{"component": "VTextField", "props": {"model": "cron", "label": "下载历史轮询 Cron", "placeholder": "留空则使用轮询间隔，如 */2 * * * *"}}],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "allowed_roots",
                                            "label": "允许入队的根目录（安全过滤器，不是主动入队列表）",
                                            "rows": 4,
                                            "placeholder": "/最近接收\n/网盘整理/分享转存目录\n留空表示不过滤",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {"model": "clouddrive2_enabled", "label": "优先按 CloudDrive2 解析"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [{"component": "VTextField", "props": {"model": "clouddrive2_prefix", "label": "CloudDrive2 前缀", "placeholder": "/115open"}}],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {"model": "share_transfer_hook_enabled", "label": "桥接STRM助手分享转存"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VTextField", "props": {"model": "share_transfer_delay", "label": "分享成功后延迟入队（秒）", "type": "number"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VTextField", "props": {"model": "recent_events_limit", "label": "事件记录数量", "type": "number"}}],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "share_transfer_enqueue_roots",
                                            "label": "分享转存成功后自动入队目录",
                                            "rows": 4,
                                            "placeholder": "/最近接收\n/网盘整理/分享转存目录\nSTRM助手报告分享转存成功后，会把这些目录逐个加入 MP 原生整理队列",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {"model": "share_roots_schedule_enabled", "label": "启用分享目录定时补漏"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [{"component": "VTextField", "props": {"model": "share_roots_schedule_cron", "label": "分享目录定时补漏 Cron", "placeholder": "例如：0 3 * * *"}}],
                            },
                        ],
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "success",
                            "variant": "tonal",
                            "density": "compact",
                            "class": "mt-2",
                            "text": "远程命令：/115bridge_poll 立即轮询；/115bridge_share 立即入队分享目录；/115bridge_status 查看状态；/115bridge_clear_cache 清理去重缓存。",
                        },
                    },
                ],
            }
        ], {
            "enabled": False,
            "cron": "",
            "interval": self.DEFAULT_INTERVAL,
            "source_username": self.DEFAULT_SOURCE_USERNAME,
            "debounce_seconds": self.DEFAULT_DEBOUNCE_SECONDS,
            "allowed_roots": "",
            "dry_run": False,
            "clouddrive2_enabled": True,
            "clouddrive2_prefix": "/115open",
            "share_transfer_hook_enabled": False,
            "share_transfer_delay": self.DEFAULT_SHARE_TRANSFER_DELAY,
            "share_transfer_enqueue_roots": "\n".join(self.DEFAULT_SHARE_TRANSFER_ENQUEUE_ROOTS),
            "share_roots_schedule_enabled": False,
            "share_roots_schedule_cron": "",
            "recent_events_limit": self.DEFAULT_RECENT_EVENTS_LIMIT,
        }

    def get_page(self) -> Optional[List[dict]]:
        """
        获取插件详情页面
        """
        runtime_state = self._load_runtime_state()
        stats = runtime_state.get("stats") or {}
        recent_events = self.get_data(self.RECENT_EVENTS_KEY) or []
        if not recent_events:
            recent_events = [
                {
                    "time": "-",
                    "status": "INFO",
                    "path": "暂无记录",
                    "message": "等待下一次运行",
                }
            ]

        summary_items = [
            f"运行状态：{'✅ 已启用' if self._enabled else '⏸️ 未启用'}",
            f"分享转存桥接：{'✅ 已启用' if self._share_transfer_hook_enabled else '⏸️ 未启用'}",
            f"来源用户：{self._source_username}",
            f"轮询方式：{self._cron or f'每 {self._interval} 秒'}",
            f"允许根目录：{len(self._allowed_roots)} 个（留空表示不过滤）",
            f"分享自动入队目录：{len(self._share_transfer_enqueue_roots)} 个",
            f"定时补漏：{'✅ 已启用 ' + self._share_roots_schedule_cron if self._share_roots_schedule_enabled and self._share_roots_schedule_cron else '⏸️ 未启用'}",
            f"去重冷却：{self._debounce_seconds} 秒",
        ]
        stats_items = [
            f"轮询次数：{stats.get('poll_runs', 0)}",
            f"手动运行：{stats.get('manual_runs', 0)}",
            f"分享转存触发：{stats.get('share_hook_success', 0)}",
            f"定时补漏：{stats.get('scheduled_share_runs', 0)}",
            f"成功入队：{stats.get('enqueue_success', 0)}",
            f"跳过：{stats.get('enqueue_skip', 0)}",
            f"错误：{stats.get('enqueue_error', 0)}",
            f"最近运行：{stats.get('last_run_time') or '-'}",
        ]

        rows = []
        for event in recent_events:
            status_code = str(event.get("status") or "-")
            rows.append(
                {
                    "component": "tr",
                    "content": [
                        {"component": "td", "text": str(event.get("time") or "-")},
                        {"component": "td", "text": self._status_text(status_code)},
                        {"component": "td", "text": str(event.get("path") or "-")},
                        {"component": "td", "text": str(event.get("message") or "-")},
                    ],
                }
            )

        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "density": "compact",
                    "class": "mb-2",
                    "text": "115整理入队桥接 v0.3.0：分享转存成功后直接入队配置目录。API：POST /api/v1/plugin/P115TransferEnqueueBridge/poll_now、/enqueue_share_roots、/clear_cache、/reset_cursor。",
                },
            },
            {
                "component": "VCard",
                "props": {"variant": "outlined", "class": "mb-2"},
                "content": [
                    {"component": "VCardTitle", "text": "运行摘要"},
                    {"component": "VCardText", "text": " ｜ ".join(summary_items)},
                ],
            },
            {
                "component": "VCard",
                "props": {"variant": "outlined", "class": "mb-2"},
                "content": [
                    {"component": "VCardTitle", "text": "累计统计"},
                    {"component": "VCardText", "text": " ｜ ".join(stats_items)},
                ],
            },
            {
                "component": "VTable",
                "props": {"hover": True},
                "content": [
                    {
                        "component": "thead",
                        "content": [
                            {
                                "component": "tr",
                                "content": [
                                    {"component": "th", "text": "时间"},
                                    {"component": "th", "text": "类型"},
                                    {"component": "th", "text": "路径"},
                                    {"component": "th", "text": "结果"},
                                ],
                            }
                        ],
                    },
                    {"component": "tbody", "content": rows},
                ],
            },
        ]

    async def _api_poll_now(self) -> Dict[str, Any]:
        try:
            result = self.run_poll_now(source="API立即运行")
            return {"code": 0, "message": "已完成下载历史轮询", "data": result}
        except Exception as err:
            logger.error(f"【115整理桥接】API立即轮询失败: {err}", exc_info=True)
            return {"code": 1, "message": str(err), "data": None}

    async def _api_enqueue_share_roots(self) -> Dict[str, Any]:
        try:
            result = self.enqueue_share_roots_now(reason="API手动触发")
            return {"code": 0, "message": "已完成分享目录入队", "data": result}
        except Exception as err:
            logger.error(f"【115整理桥接】API分享目录入队失败: {err}", exc_info=True)
            return {"code": 1, "message": str(err), "data": None}

    async def _api_reset_cursor(self) -> Dict[str, Any]:
        try:
            result = self.reset_cursor()
            return {"code": 0, "message": "已重置游标", "data": result}
        except Exception as err:
            logger.error(f"【115整理桥接】API重置游标失败: {err}", exc_info=True)
            return {"code": 1, "message": str(err), "data": None}

    async def _api_clear_cache(self) -> Dict[str, Any]:
        try:
            result = self.clear_path_cache()
            return {"code": 0, "message": "已清理去重缓存", "data": result}
        except Exception as err:
            logger.error(f"【115整理桥接】API清理缓存失败: {err}", exc_info=True)
            return {"code": 1, "message": str(err), "data": None}

    async def _api_status(self) -> Dict[str, Any]:
        return {"code": 0, "message": "success", "data": self._status_summary()}

    @eventmanager.register(EventType.PluginAction)
    def remote_action(self, event: Event = None):
        if not event or not event.event_data:
            return
        action = event.event_data.get("action")
        if action not in {"p115bridge_poll", "p115bridge_share", "p115bridge_status", "p115bridge_clear_cache"}:
            return
        channel = event.event_data.get("channel")
        userid = event.event_data.get("user")
        try:
            if action == "p115bridge_poll":
                result = self.run_poll_now(source="远程命令")
                title = "115整理桥接：立即轮询完成"
                text = f"入队 {result.get('enqueued', 0)}，跳过 {result.get('skipped', 0)}"
            elif action == "p115bridge_share":
                result = self.enqueue_share_roots_now(reason="远程命令")
                title = "115整理桥接：分享目录入队完成"
                text = f"成功 {result.get('enqueued', 0)}，跳过 {result.get('skipped', 0)}，失败 {result.get('errors', 0)}"
            elif action == "p115bridge_clear_cache":
                result = self.clear_path_cache()
                title = "115整理桥接：缓存已清理"
                text = f"清理路径数：{result.get('cleared', 0)}"
            else:
                summary = self._status_summary()
                title = "115整理桥接状态"
                text = "\n".join(f"{k}: {v}" for k, v in summary.items())
            self.post_message(channel=channel, title=title, text=text, userid=userid)
        except Exception as err:
            logger.error(f"【115整理桥接】远程命令执行失败: {err}", exc_info=True)
            self.post_message(channel=channel, title="115整理桥接：远程命令失败", text=str(err), userid=userid)

    def run_poll_now(self, source: str = "手动触发") -> Dict[str, int]:
        self._stats_increment("manual_runs")
        return self.poll_download_history(source=source)

    def reset_cursor(self) -> Dict[str, Any]:
        runtime_state = self._load_runtime_state()
        runtime_state["cursor_state"] = {}
        self._save_runtime_state(runtime_state)
        self._record_recent_event("CURSOR", "-", "下载历史游标已重置；下次运行会重新建立游标")
        return {"reset": True}

    def clear_path_cache(self) -> Dict[str, int]:
        runtime_state = self._load_runtime_state()
        path_cache = runtime_state.get("path_cache") or {}
        cleared = len(path_cache)
        runtime_state["path_cache"] = {}
        self._save_runtime_state(runtime_state)
        self._record_recent_event("CACHE", "-", f"已清理去重缓存，共 {cleared} 条")
        return {"cleared": cleared}

    def _status_summary(self) -> Dict[str, Any]:
        runtime_state = self._load_runtime_state()
        stats = runtime_state.get("stats") or {}
        return {
            "enabled": self._enabled,
            "version": self.plugin_version,
            "source_username": self._source_username,
            "interval": self._interval,
            "cron": self._cron,
            "share_transfer_hook_enabled": self._share_transfer_hook_enabled,
            "share_transfer_enqueue_roots": [str(path) for path in self._share_transfer_enqueue_roots],
            "share_roots_schedule_enabled": self._share_roots_schedule_enabled,
            "share_roots_schedule_cron": self._share_roots_schedule_cron,
            "stats": stats,
        }

    def stop_service(self):
        """
        停止插件服务
        """
        self._restore_share_transfer_hook()

    def poll_download_history(self, source: str = "定时轮询") -> Dict[str, int]:
        """
        轮询 DownloadHistory 并加入整理队列
        """
        self._ensure_share_transfer_hook()
        self._stats_increment("poll_runs")

        try:
            records, table_info = self._fetch_recent_records()
        except Exception as err:
            logger.error(f"【115整理桥接】读取下载历史失败: {err}", exc_info=True)
            self._record_recent_event("ERROR", "-", f"读取下载历史失败: {err}")
            self._stats_increment("enqueue_error")
            return {"enqueued": 0, "skipped": 0, "errors": 1}

        if not records:
            logger.debug("【115整理桥接】未查询到来源用户 %s 的下载历史", self._source_username)
            return {"enqueued": 0, "skipped": 0, "errors": 0}

        cursor_key = table_info.get("cursor_col") or ""
        path_key = table_info.get("path_col") or "path"
        runtime_state = self._load_runtime_state()
        cursor_state = runtime_state.get("cursor_state") or {}
        current_signature = f"{table_info.get('table')}:{cursor_key}"

        if cursor_state.get("signature") != current_signature:
            cursor_state = {"signature": current_signature, "value": None}

        latest_record = records[0]
        latest_cursor = self._get_record_cursor(latest_record, table_info)
        if cursor_state.get("value") is None:
            cursor_state["value"] = latest_cursor
            runtime_state["cursor_state"] = cursor_state
            self._save_runtime_state(runtime_state)
            logger.info("【115整理桥接】首次运行已建立游标，不回补已有历史记录")
            self._record_recent_event("CURSOR", "-", "首次运行已建立游标，不回补已有历史记录")
            return {"enqueued": 0, "skipped": 0, "errors": 0}

        new_records = []
        for record in reversed(records):
            if self._is_newer_cursor(
                self._get_record_cursor(record, table_info),
                cursor_state.get("value"),
            ):
                new_records.append(record)

        if not new_records:
            logger.debug("【115整理桥接】未发现新的下载历史记录")
            self._update_last_run_time()
            return {"enqueued": 0, "skipped": 0, "errors": 0}

        path_cache = runtime_state.get("path_cache") or {}
        now_ts = int(time())
        queued_paths = set()
        handled_count = 0
        skipped_count = 0

        for record in new_records:
            cursor_state["value"] = self._get_record_cursor(record, table_info)
            raw_path = record.get(path_key)
            normalized_path = self._normalize_path(raw_path)
            if not normalized_path:
                skipped_count += 1
                self._record_recent_event("SKIP", "-", "记录 path 为空，已跳过")
                continue
            if normalized_path in queued_paths:
                skipped_count += 1
                self._record_recent_event("SKIP", normalized_path, "本轮已处理同路径记录")
                continue
            if not self._is_path_allowed(Path(normalized_path)):
                skipped_count += 1
                logger.info("【115整理桥接】路径不在允许根目录内，跳过: %s", normalized_path)
                self._record_recent_event("SKIP", normalized_path, "路径不在允许根目录内")
                queued_paths.add(normalized_path)
                continue
            if not self._should_process_path(normalized_path, path_cache, now_ts):
                skipped_count += 1
                logger.info("【115整理桥接】路径仍处于去重冷却中，跳过: %s", normalized_path)
                self._record_recent_event("SKIP", normalized_path, "仍处于去重冷却中")
                queued_paths.add(normalized_path)
                continue

            if self._enqueue_path(normalized_path):
                handled_count += 1
                path_cache[normalized_path] = now_ts
            else:
                skipped_count += 1
            queued_paths.add(normalized_path)

        runtime_state["cursor_state"] = cursor_state
        runtime_state["path_cache"] = self._trim_path_cache(path_cache, now_ts)
        self._save_runtime_state(runtime_state)
        self._stats_increment("enqueue_success", handled_count)
        self._stats_increment("enqueue_skip", skipped_count)
        self._update_last_run_time()
        logger.info(
            "【115整理桥接】本轮处理完成 新记录=%s 入队=%s 跳过=%s",
            len(new_records),
            handled_count,
            skipped_count,
        )
        self._record_recent_event("DONE", "-", f"{source}完成：新记录 {len(new_records)}，入队 {handled_count}，跳过 {skipped_count}")
        return {"enqueued": handled_count, "skipped": skipped_count, "errors": 0}

    def _ensure_share_transfer_hook(self) -> None:
        """
        按需包装 P115StrmHelper 的分享转存函数。
        """
        if not self._enabled or not self._share_transfer_hook_enabled:
            self._restore_share_transfer_hook()
            return

        with self._share_transfer_hook_lock:
            try:
                from app.plugins.p115strmhelper.service import servicer
            except Exception as err:
                logger.debug(f"【115整理桥接】暂无法导入 P115StrmHelper servicer，稍后重试: {err}")
                return

            helper = getattr(servicer, "sharetransferhelper", None)
            if helper is None:
                logger.debug("【115整理桥接】P115StrmHelper sharetransferhelper 尚未初始化，稍后重试")
                return

            current_func = getattr(helper, "add_share_115", None)
            if current_func is None:
                logger.warning("【115整理桥接】P115StrmHelper 未找到 add_share_115，无法桥接分享转存")
                return

            original_func = getattr(helper, "_p115_bridge_original_add_share_115", None)
            if original_func is None:
                original_func = current_func
            elif getattr(helper, "_p115_bridge_owner", None) is self:
                return

            bridge = self

            def wrapped_add_share_115(*args, **kwargs):
                result = original_func(*args, **kwargs)
                bridge._schedule_share_transfer_roots_enqueue(result)
                return result

            setattr(helper, "add_share_115", wrapped_add_share_115)
            setattr(helper, "_p115_bridge_original_add_share_115", original_func)
            setattr(helper, "_p115_bridge_owner", self)
            self._share_transfer_hooked_helper = helper
            logger.info("【115整理桥接】已启用 P115StrmHelper 分享转存成功桥接")

    def _restore_share_transfer_hook(self) -> None:
        """
        恢复被包装的 P115StrmHelper 分享转存函数。
        """
        with self._share_transfer_hook_lock:
            helper = self._share_transfer_hooked_helper
            if helper is None:
                return
            if getattr(helper, "_p115_bridge_owner", None) is not self:
                self._share_transfer_hooked_helper = None
                return
            original_func = getattr(helper, "_p115_bridge_original_add_share_115", None)
            if original_func is not None:
                try:
                    setattr(helper, "add_share_115", original_func)
                    delattr(helper, "_p115_bridge_original_add_share_115")
                    delattr(helper, "_p115_bridge_owner")
                    logger.info("【115整理桥接】已恢复 P115StrmHelper 分享转存函数")
                except Exception as err:
                    logger.debug(f"【115整理桥接】恢复分享转存函数失败: {err}")
            self._share_transfer_hooked_helper = None

    def _schedule_share_transfer_roots_enqueue(self, result: Any) -> None:
        """
        STRM助手分享转存成功后，延迟将配置目录加入整理队列。
        """
        if not self._is_share_transfer_success(result):
            return
        self._stats_increment("share_hook_success")
        worker = Thread(
            target=self._delayed_enqueue_share_transfer_roots,
            args=("分享转存成功",),
            name="P115TransferEnqueueBridge-ShareRoots",
            daemon=True,
        )
        worker.start()

    @staticmethod
    def _is_share_transfer_success(result: Any) -> bool:
        """
        判断 P115StrmHelper.add_share_115 返回值是否表示成功。
        """
        return isinstance(result, tuple) and len(result) >= 1 and bool(result[0])

    def _delayed_enqueue_share_transfer_roots(self, reason: str) -> None:
        delay = max(self._share_transfer_delay, 0)
        if delay:
            sleep(delay)
        self._enqueue_share_transfer_roots(reason=reason)

    def enqueue_share_roots_scheduled(self):
        """
        分享目录定时补漏入口。
        """
        self._stats_increment("scheduled_share_runs")
        return self._enqueue_share_transfer_roots(reason="定时补漏")

    def enqueue_share_roots_now(self, reason: str = "手动触发") -> Dict[str, int]:
        """
        手动立即入队分享转存配置目录。
        """
        self._stats_increment("manual_runs")
        return self._enqueue_share_transfer_roots(reason=reason)

    def _get_share_transfer_enqueue_roots(self) -> List[str]:
        """
        获取分享转存成功后主动入队目录。
        """
        roots: List[str] = []
        configured_roots = self._share_transfer_enqueue_roots or self._parse_allowed_roots(
            "\n".join(self.DEFAULT_SHARE_TRANSFER_ENQUEUE_ROOTS)
        )
        for root in configured_roots:
            normalized_root = self._normalize_path(root)
            if normalized_root and normalized_root not in roots:
                roots.append(normalized_root)
        return roots

    def _enqueue_share_transfer_roots(self, reason: str) -> Dict[str, int]:
        """
        将配置的分享转存目录逐个加入 MP 原生整理队列。
        """
        if not self._runtime_lock.acquire(blocking=False):
            self._record_recent_event("SHARE-SKIP", "-", f"{reason}：已有任务运行中，已跳过")
            self._stats_increment("enqueue_skip")
            return {"enqueued": 0, "skipped": 1, "errors": 0}
        try:
            runtime_state = self._load_runtime_state()
            path_cache = runtime_state.get("path_cache") or {}
            now_ts = int(time())
            enqueued = 0
            skipped = 0
            errors = 0
            roots = self._get_share_transfer_enqueue_roots()
            if not roots:
                self._record_recent_event("SHARE-SKIP", "-", f"{reason}：未配置分享转存自动入队目录")
                self._stats_increment("enqueue_skip")
                return {"enqueued": 0, "skipped": 1, "errors": 0}

            for root in roots:
                normalized_path = self._normalize_path(root)
                if not normalized_path:
                    skipped += 1
                    continue
                if not self._is_path_allowed(Path(normalized_path)):
                    skipped += 1
                    self._record_recent_event("SHARE-SKIP", normalized_path, f"{reason}：路径不在允许根目录内")
                    continue
                if not self._should_process_path(normalized_path, path_cache, now_ts):
                    skipped += 1
                    self._record_recent_event("SHARE-SKIP", normalized_path, f"{reason}：仍处于去重冷却中")
                    continue
                if self._enqueue_path(normalized_path):
                    enqueued += 1
                    path_cache[normalized_path] = now_ts
                    self._record_recent_event("SHARE-ENQUEUE", normalized_path, f"{reason}：已加入整理队列")
                else:
                    errors += 1

            runtime_state["path_cache"] = self._trim_path_cache(path_cache, now_ts)
            stats = runtime_state.get("stats") or {}
            stats["last_run_time"] = self._now_text()
            stats["last_share_enqueue_time"] = self._now_text()
            stats["enqueue_success"] = self._safe_int(stats.get("enqueue_success"), 0) + enqueued
            stats["enqueue_skip"] = self._safe_int(stats.get("enqueue_skip"), 0) + skipped
            stats["enqueue_error"] = self._safe_int(stats.get("enqueue_error"), 0) + errors
            runtime_state["stats"] = stats
            self._save_runtime_state(runtime_state)
            self._record_recent_event(
                "SHARE-DONE",
                "-",
                f"{reason}完成：成功 {enqueued}，跳过 {skipped}，失败 {errors}",
            )
            return {"enqueued": enqueued, "skipped": skipped, "errors": errors}
        finally:
            self._runtime_lock.release()

    def _build_trigger(self):
        """
        构建调度触发器
        """
        if self._cron:
            try:
                return CronTrigger.from_crontab(self._cron)
            except Exception as err:
                logger.error(f"【115整理桥接】Cron 表达式无效，改用 interval: {err}")

        interval = max(self._interval, 10)
        return IntervalTrigger(seconds=interval)

    def _fetch_recent_records(self) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
        """
        查询近期下载历史记录
        """
        with SessionFactory() as db:
            table_info = self._resolve_table_info(db)
            table_name = table_info["table"]
            username_col = table_info["username_col"]
            path_col = table_info["path_col"]
            cursor_col = table_info.get("cursor_col") or path_col

            query = (
                f"SELECT * FROM {self._quote_name(table_name)} "
                f"WHERE {self._quote_name(username_col)} = :username "
                f"AND COALESCE({self._quote_name(path_col)}, '') != '' "
                f"ORDER BY {self._quote_name(cursor_col)} DESC "
                f"LIMIT :limit"
            )
            rows = db.execute(
                text(query),
                {"username": self._source_username, "limit": self.DEFAULT_HISTORY_LIMIT},
            ).mappings().all()
            return [dict(row) for row in rows], table_info

    def _resolve_table_info(self, db) -> Dict[str, str]:
        """
        推断下载历史表与关键字段
        """
        inspector = inspect(db.get_bind())
        table_names = inspector.get_table_names()
        table_name = self._pick_first(
            table_names,
            ["downloadhistory", "download_history"],
        )
        if not table_name:
            for name in table_names:
                lowered = name.lower()
                if "download" in lowered and "history" in lowered:
                    table_name = name
                    break
        if not table_name:
            raise RuntimeError("未找到 DownloadHistory 对应数据表")

        columns = [column.get("name") for column in inspector.get_columns(table_name)]
        username_col = self._pick_first(
            columns,
            ["username", "user_name", "source_username", "source"],
        )
        path_col = self._pick_first(
            columns,
            ["path", "fullpath", "full_path", "save_path", "save_dir"],
        )
        cursor_col = self._pick_first(
            columns,
            ["id", "created_at", "create_time", "ctime", "created", "add_time"],
        )

        if not username_col or not path_col:
            raise RuntimeError(
                f"下载历史表缺少必要字段 username/path，当前字段: {', '.join(columns)}"
            )

        return {
            "table": table_name,
            "username_col": username_col,
            "path_col": path_col,
            "cursor_col": cursor_col or path_col,
        }

    def _enqueue_path(self, normalized_path: str) -> bool:
        """
        将路径加入 MoviePilot 原生整理队列
        """
        file_item = self._build_file_item(normalized_path)
        if not file_item:
            logger.warning("【115整理桥接】无法构建 FileItem，跳过: %s", normalized_path)
            self._record_recent_event("SKIP", normalized_path, "无法构建 FileItem")
            return False

        if self._dry_run:
            logger.info(
                "【115整理桥接】dry_run 模式，模拟加入整理队列: %s (%s/%s)",
                normalized_path,
                file_item.storage,
                file_item.type,
            )
            self._record_recent_event(
                "DRYRUN",
                normalized_path,
                f"模拟入队 {file_item.storage}/{file_item.type}",
            )
            return True

        try:
            transferchain = self._transferchain or TransferChain()
            transferchain.do_transfer(fileitem=file_item)
            logger.info(
                "【115整理桥接】已加入整理队列: %s (%s/%s)",
                normalized_path,
                file_item.storage,
                file_item.type,
            )
            self._record_recent_event(
                "ENQUEUE",
                normalized_path,
                f"已入队 {file_item.storage}/{file_item.type}",
            )
            return True
        except Exception as err:
            logger.error(
                f"【115整理桥接】调用 TransferChain.do_transfer 失败: {normalized_path} - {err}",
                exc_info=True,
            )
            self._record_recent_event("ERROR", normalized_path, f"入队失败: {err}")
            return False

    def _build_file_item(self, normalized_path: str) -> Optional[FileItem]:
        """
        优先按 CloudDrive2 路径解析 FileItem，失败后再尝试本地路径
        """
        storagechain = self._storagechain or StorageChain()

        if self._clouddrive2_enabled:
            cd2_item = self._build_clouddrive2_file_item(normalized_path)
            if cd2_item:
                return cd2_item

        path_obj = Path(normalized_path)
        if not path_obj.exists():
            logger.warning("【115整理桥接】本地路径不存在，且未解析到 CloudDrive 项: %s", normalized_path)
            return None

        try:
            file_item = storagechain.get_file_item(storage="local", path=path_obj)
            if file_item:
                return file_item
        except Exception as err:
            logger.debug(f"【115整理桥接】StorageChain.get_file_item(local) 失败，改用本地兜底: {err}")

        stat_result = path_obj.stat()
        if path_obj.is_dir():
            return FileItem(
                storage="local",
                type="dir",
                path=path_obj.as_posix(),
                name=path_obj.name or path_obj.as_posix(),
                basename=path_obj.stem or path_obj.name or path_obj.as_posix(),
                modify_time=stat_result.st_mtime,
            )

        return FileItem(
            storage="local",
            type="file",
            path=path_obj.as_posix(),
            name=path_obj.name,
            basename=path_obj.stem,
            extension=path_obj.suffix[1:].lower(),
            size=stat_result.st_size,
            modify_time=stat_result.st_mtime,
        )

    def _build_clouddrive2_file_item(self, normalized_path: str) -> Optional[FileItem]:
        """
        将 /网盘整理/... 路径映射为 CloudDrive储存 FileItem
        """
        prefix = self._normalize_path(self._clouddrive2_prefix)
        if not prefix:
            return None

        storagechain = self._storagechain or StorageChain()
        source_path = Path(normalized_path)
        cd2_path = Path(prefix) / Path(*source_path.parts[1:]) if source_path.is_absolute() else Path(prefix) / source_path
        cd2_path_str = cd2_path.as_posix()

        try:
            if source_path.suffix:
                return resolve_file_via_parent_list(
                    storagechain,
                    "CloudDrive储存",
                    cd2_path,
                    log_label="【115整理桥接】",
                )
            return resolve_directory_via_parent_list(
                storagechain,
                "CloudDrive储存",
                cd2_path,
                log_label="【115整理桥接】",
            )
        except Exception as err:
            logger.debug(f"【115整理桥接】CloudDrive2 路径解析失败: {normalized_path} -> {cd2_path_str} err={err}")
            return None

    def _load_runtime_state(self) -> Dict[str, Any]:
        """
        读取运行态缓存
        """
        runtime_state = self.get_data(self.RUNTIME_STATE_KEY) or {}
        if not isinstance(runtime_state, dict):
            runtime_state = {}
        if not isinstance(runtime_state.get("path_cache"), dict):
            runtime_state["path_cache"] = {}
        if not isinstance(runtime_state.get("cursor_state"), dict):
            runtime_state["cursor_state"] = {}
        if not isinstance(runtime_state.get("stats"), dict):
            runtime_state["stats"] = {}
        return runtime_state

    def _save_runtime_state(self, runtime_state: Dict[str, Any]) -> None:
        """
        保存运行态缓存
        """
        self.save_data(self.RUNTIME_STATE_KEY, runtime_state)

    def _record_recent_event(self, status: str, path: str, message: str) -> None:
        """
        记录最近事件
        """
        recent_events = self.get_data(self.RECENT_EVENTS_KEY) or []
        if not isinstance(recent_events, list):
            recent_events = []
        recent_events.insert(
            0,
            {
                "time": self._now_text(),
                "timestamp": int(time()),
                "status": status,
                "path": path,
                "message": message,
            },
        )
        self.save_data(self.RECENT_EVENTS_KEY, recent_events[: self._recent_events_limit])

    @staticmethod
    def _now_text() -> str:
        return datetime.now().strftime("%m-%d %H:%M:%S")

    def _status_text(self, status: str) -> str:
        return self.STATUS_TEXT_MAP.get(str(status or ""), str(status or "-"))

    def _stats_increment(self, key: str, count: int = 1) -> None:
        runtime_state = self._load_runtime_state()
        stats = runtime_state.get("stats") or {}
        stats[key] = self._safe_int(stats.get(key), 0) + count
        stats["last_run_time"] = self._now_text()
        runtime_state["stats"] = stats
        self._save_runtime_state(runtime_state)

    def _update_last_run_time(self) -> None:
        runtime_state = self._load_runtime_state()
        stats = runtime_state.get("stats") or {}
        stats["last_run_time"] = self._now_text()
        runtime_state["stats"] = stats
        self._save_runtime_state(runtime_state)

    def _trim_path_cache(
        self,
        path_cache: Dict[str, Any],
        now_ts: int,
    ) -> Dict[str, int]:
        """
        裁剪路径去重缓存
        """
        expire_seconds = max(self._debounce_seconds * 10, 86400)
        trimmed_cache: Dict[str, int] = {}
        for path, last_ts in path_cache.items():
            last_ts_int = self._safe_int(last_ts, 0)
            if last_ts_int and now_ts - last_ts_int <= expire_seconds:
                trimmed_cache[path] = last_ts_int
        return trimmed_cache

    def _should_process_path(
        self,
        normalized_path: str,
        path_cache: Dict[str, Any],
        now_ts: int,
    ) -> bool:
        """
        判断路径是否满足去重冷却
        """
        last_ts = self._safe_int(path_cache.get(normalized_path), 0)
        if not last_ts:
            return True
        return now_ts - last_ts >= max(self._debounce_seconds, 0)

    def _is_path_allowed(self, path_obj: Path) -> bool:
        """
        判断路径是否在允许根目录中
        """
        if not self._allowed_roots:
            return True

        normalized_path = self._normalize_path(path_obj)
        if not normalized_path:
            return False
        candidate = Path(normalized_path)

        for root in self._allowed_roots:
            if candidate == root or self._is_relative_to(candidate, root):
                return True
        return False

    @staticmethod
    def _parse_allowed_roots(allowed_roots_text: str) -> List[Path]:
        """
        解析允许根目录配置
        """
        roots: List[Path] = []
        for line in (allowed_roots_text or "").splitlines():
            normalized = P115TransferEnqueueBridge._normalize_path(line)
            if normalized:
                roots.append(Path(normalized))
        return roots

    @staticmethod
    def _normalize_path(path_value: Any) -> str:
        """
        归一化路径字符串
        """
        if not path_value:
            return ""
        raw_path = str(path_value).strip()
        if not raw_path:
            return ""
        return Path(raw_path).expanduser().resolve(strict=False).as_posix()

    @staticmethod
    def _get_record_cursor(record: Dict[str, Any], table_info: Dict[str, str]) -> Any:
        """
        提取记录游标值
        """
        return record.get(table_info.get("cursor_col") or "")

    @staticmethod
    def _coerce_cursor(cursor_value: Any) -> Any:
        """
        归一化游标值
        """
        if cursor_value is None:
            return None
        if isinstance(cursor_value, (int, float)):
            return cursor_value
        cursor_text = str(cursor_value).strip()
        if not cursor_text:
            return None
        try:
            return int(cursor_text)
        except Exception:
            pass
        try:
            return float(cursor_text)
        except Exception:
            pass
        return cursor_text

    @classmethod
    def _is_newer_cursor(cls, cursor_value: Any, last_value: Any) -> bool:
        """
        比较记录游标是否更新
        """
        current = cls._coerce_cursor(cursor_value)
        previous = cls._coerce_cursor(last_value)
        if current is None:
            return False
        if previous is None:
            return True
        try:
            return current > previous
        except TypeError:
            return str(current) > str(previous)

    @staticmethod
    def _pick_first(candidates: List[str], preferred: List[str]) -> Optional[str]:
        """
        从候选集中挑选首个匹配字段
        """
        lowered_mapping = {str(candidate).lower(): candidate for candidate in candidates}
        for key in preferred:
            if key.lower() in lowered_mapping:
                return lowered_mapping[key.lower()]
        return None

    @staticmethod
    def _quote_name(name: str) -> str:
        """
        为 SQL 标识符加引号
        """
        escaped_name = str(name).replace('"', '""')
        return f'"{escaped_name}"'

    @staticmethod
    def _safe_int(value: Any, default: int) -> int:
        """
        安全转换整数
        """
        try:
            return int(value)
        except Exception:
            return default

    @staticmethod
    def _is_relative_to(path_obj: Path, root_obj: Path) -> bool:
        """
        兼容旧版本 Path.is_relative_to 的前缀判断
        """
        try:
            return path_obj.is_relative_to(root_obj)
        except AttributeError:
            try:
                path_obj.relative_to(root_obj)
                return True
            except ValueError:
                return False
        except ValueError:
            return False
