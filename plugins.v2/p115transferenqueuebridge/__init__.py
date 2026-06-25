from pathlib import Path
from threading import Lock, Thread
from time import sleep, time
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import inspect, text

from app.chain.storage import StorageChain
from app.chain.transfer import TransferChain
from app.db import SessionFactory
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import FileItem

from app.plugins.p115strmhelper.utils.storage_item import (
    resolve_directory_via_parent_list,
    resolve_file_via_parent_list,
)


class P115TransferEnqueueBridge(_PluginBase):
    """
    115 下载历史整理桥接插件

    轮询 DownloadHistory 中指定来源用户的新记录，按 path 去重后直接调用 MoviePilot 原生 TransferChain.do_transfer。
    可选包装 P115StrmHelper 分享转存成功回调，仅在分享转存成功后延迟做一次轻量差异检测并入队，
    避免常驻扫描 115 目录。
    """

    plugin_name = "115整理入队桥接"
    plugin_desc = "轮询115下载历史，并可按需桥接115网盘STRM助手分享转存到原生整理队列"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/cloud.png"
    plugin_version = "0.2.1"
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
    DEFAULT_SHARE_TRANSFER_MAX_NEW_ITEMS = 10
    RUNTIME_STATE_KEY = "runtime_state"
    RECENT_EVENTS_KEY = "recent_events"
    RECENT_EVENTS_LIMIT = 20

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
    _share_transfer_max_new_items: int = DEFAULT_SHARE_TRANSFER_MAX_NEW_ITEMS
    _share_transfer_hook_lock = Lock()
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
        self._share_transfer_max_new_items = max(
            self._safe_int(
                config.get("share_transfer_max_new_items"),
                self.DEFAULT_SHARE_TRANSFER_MAX_NEW_ITEMS,
            ),
            1,
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
                "share_transfer_max_new_items": self._share_transfer_max_new_items,
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
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件 API
        """
        return []

    def get_service(self) -> List[Dict[str, Any]] | None:
        """
        注册插件公共服务
        """
        if not self._enabled:
            return None

        self._ensure_share_transfer_hook()

        trigger = self._build_trigger()
        if not trigger:
            return None

        return [
            {
                "id": "P115TransferEnqueueBridge_poll",
                "name": "115下载历史整理入队桥接",
                "trigger": trigger,
                "func": self.poll_download_history,
                "kwargs": {},
            }
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面
        """
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "dry_run",
                                            "label": "仅日志演练",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "interval",
                                            "label": "轮询间隔（秒）",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "debounce_seconds",
                                            "label": "去重冷却（秒）",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "source_username",
                                            "label": "来源用户名",
                                            "placeholder": "P115StrgmSub",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "cron",
                                            "label": "Cron 表达式",
                                            "placeholder": "留空则使用轮询间隔，如 */2 * * * *",
                                        },
                                    }
                                ],
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
                                            "label": "允许入队的根目录",
                                            "rows": 5,
                                            "placeholder": "/网盘整理/网盘待整理目录/Movie\n/网盘整理/网盘待整理目录/TV\n留空表示不过滤",
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
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "clouddrive2_enabled",
                                            "label": "优先按 CloudDrive2 解析",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "clouddrive2_prefix",
                                            "label": "CloudDrive2 前缀",
                                            "placeholder": "/115open",
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
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "share_transfer_hook_enabled",
                                            "label": "桥接STRM助手分享转存",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "share_transfer_delay",
                                            "label": "转存成功后延迟检测（秒）",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "share_transfer_max_new_items",
                                            "label": "每次最多入队新增项",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "density": "compact",
                            "class": "mt-2",
                            "text": (
                                "插件会轮询 DownloadHistory 中 source_username 对应的新记录，"
                                "按 path 去重后调用 MoviePilot 原生整理队列。"
                                "开启分享转存桥接后，仅包装115网盘STRM助手分享转存成功返回值，"
                                "成功后延迟做一次目录差异检测，不做常驻目录扫描。"
                                "Cron 优先于 interval，首次运行默认只建立游标，不自动回补历史记录。"
                            ),
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
            "share_transfer_max_new_items": self.DEFAULT_SHARE_TRANSFER_MAX_NEW_ITEMS,
        }

    def get_page(self) -> Optional[List[dict]]:
        """
        获取插件详情页面
        """
        recent_events = self.get_data(self.RECENT_EVENTS_KEY) or []
        if not recent_events:
            recent_events = [
                {
                    "time": "-",
                    "status": "INFO",
                    "path": "暂无记录",
                    "message": "等待下一次轮询",
                }
            ]

        rows = []
        for event in recent_events:
            rows.append(
                {
                    "component": "tr",
                    "content": [
                        {"component": "td", "text": str(event.get("time") or "-")},
                        {"component": "td", "text": str(event.get("status") or "-")},
                        {"component": "td", "text": str(event.get("path") or "-")},
                        {"component": "td", "text": str(event.get("message") or "-")},
                    ],
                }
            )

        return [
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
                                    {"component": "th", "text": "状态"},
                                    {"component": "th", "text": "路径"},
                                    {"component": "th", "text": "说明"},
                                ],
                            }
                        ],
                    },
                    {
                        "component": "tbody",
                        "content": rows,
                    },
                ],
            }
        ]

    def stop_service(self):
        """
        停止插件服务
        """
        self._restore_share_transfer_hook()

    def poll_download_history(self):
        """
        轮询 DownloadHistory 并加入整理队列
        """
        self._ensure_share_transfer_hook()

        try:
            records, table_info = self._fetch_recent_records()
        except Exception as err:
            logger.error(f"【115整理桥接】读取下载历史失败: {err}", exc_info=True)
            self._record_recent_event("ERROR", "-", f"读取下载历史失败: {err}")
            return

        if not records:
            logger.debug("【115整理桥接】未查询到来源用户 %s 的下载历史", self._source_username)
            return

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
            self._record_recent_event("INFO", "-", "首次运行已建立游标")
            return

        new_records = []
        for record in reversed(records):
            if self._is_newer_cursor(
                self._get_record_cursor(record, table_info),
                cursor_state.get("value"),
            ):
                new_records.append(record)

        if not new_records:
            logger.debug("【115整理桥接】未发现新的下载历史记录")
            return

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
        logger.info(
            "【115整理桥接】本轮处理完成 新记录=%s 入队=%s 跳过=%s",
            len(new_records),
            handled_count,
            skipped_count,
        )

    def _ensure_share_transfer_hook(self) -> None:
        """
        按需包装 P115StrmHelper 的分享转存函数。

        该包装只读取 add_share_115 的成功返回值，不修改 STRM助手源码，不常驻扫描115目录。
        成功后在后台线程延迟做一次目标目录差异检测，将新增直接子项加入原生整理队列。
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
                before_snapshot = bridge._snapshot_share_parent_before_transfer(args, kwargs)
                result = original_func(*args, **kwargs)
                bridge._schedule_share_transfer_enqueue(result, before_snapshot)
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

    def _snapshot_share_parent_before_transfer(
        self,
        args: Tuple[Any, ...],
        kwargs: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        分享转存前读取目标父目录的一层快照。
        """
        parent_path = self._extract_share_transfer_parent_path(args, kwargs)
        if not parent_path:
            return {"ok": False, "parent_path": "", "items": {}}
        items = self._snapshot_share_parent(parent_path)
        return {"ok": bool(items is not None), "parent_path": parent_path, "items": items or {}}

    def _extract_share_transfer_parent_path(
        self,
        args: Tuple[Any, ...],
        kwargs: Dict[str, Any],
    ) -> str:
        """
        从 add_share_115 调用参数中推断本次分享转存目录。
        """
        pan_path = kwargs.get("pan_path")
        if not pan_path and len(args) >= 6:
            pan_path = args[5]
        if pan_path:
            return self._normalize_path(pan_path)

        try:
            from app.plugins.p115strmhelper.core.config import configer

            paths = configer.share_recieve_paths or []
            if paths:
                return self._normalize_path(paths[0])
        except Exception as err:
            logger.debug(f"【115整理桥接】读取 P115StrmHelper 分享转存默认目录失败: {err}")
        return ""

    def _schedule_share_transfer_enqueue(
        self,
        result: Any,
        before_snapshot: Dict[str, Any],
    ) -> None:
        """
        分享转存成功后启动后台线程延迟检测新增项。
        """
        if not self._is_share_transfer_success(result):
            return

        result_parent_path = self._extract_share_transfer_parent_path_from_result(result)
        parent_path = result_parent_path or str(before_snapshot.get("parent_path") or "")
        if not parent_path:
            self._record_recent_event("SHARE-SKIP", "-", "分享转存成功但无法确定转存目录")
            return

        if not before_snapshot.get("ok"):
            self._record_recent_event(
                "SHARE-SKIP",
                parent_path,
                "分享转存前快照失败，为避免误扫历史目录，本次不自动入队",
            )
            return

        worker = Thread(
            target=self._delayed_enqueue_share_transfer,
            args=(parent_path, dict(before_snapshot.get("items") or {})),
            name="P115TransferEnqueueBridge-ShareTransfer",
            daemon=True,
        )
        worker.start()

    @staticmethod
    def _is_share_transfer_success(result: Any) -> bool:
        """
        判断 P115StrmHelper.add_share_115 返回值是否表示成功。
        """
        return isinstance(result, tuple) and len(result) >= 1 and bool(result[0])

    def _extract_share_transfer_parent_path_from_result(self, result: Any) -> str:
        """
        从 P115StrmHelper.add_share_115 成功返回值中提取转存目录。
        当前返回结构为 (True, file_mediainfo, parent_path, parent_id)。
        """
        if isinstance(result, tuple) and len(result) >= 3:
            return self._normalize_path(result[2])
        return ""

    def _delayed_enqueue_share_transfer(
        self,
        parent_path: str,
        before_items: Dict[str, str],
    ) -> None:
        """
        延迟一次性检测分享转存目录新增子项并入队。
        """
        delay = max(self._share_transfer_delay, 0)
        if delay:
            sleep(delay)

        after_items = self._snapshot_share_parent(parent_path)
        if after_items is None:
            self._record_recent_event("SHARE-ERROR", parent_path, "分享转存后快照失败")
            return

        new_paths = [path for path in after_items.keys() if path not in before_items]
        if not new_paths:
            self._record_recent_event("SHARE-SKIP", parent_path, "分享转存成功但未检测到新增子项")
            return

        if len(new_paths) > self._share_transfer_max_new_items:
            self._record_recent_event(
                "SHARE-SKIP",
                parent_path,
                f"检测到新增项 {len(new_paths)} 个，超过上限 {self._share_transfer_max_new_items}，为避免误入队已跳过",
            )
            return

        path_cache = {}
        try:
            runtime_state = self._load_runtime_state()
            path_cache = runtime_state.get("path_cache") or {}
        except Exception:
            runtime_state = {"path_cache": {}}

        now_ts = int(time())
        enqueued = 0
        skipped = 0
        for path in sorted(new_paths):
            normalized_path = self._normalize_path(path)
            if not normalized_path:
                skipped += 1
                continue
            if not self._is_path_allowed(Path(normalized_path)):
                skipped += 1
                self._record_recent_event("SHARE-SKIP", normalized_path, "路径不在允许根目录内")
                continue
            if not self._should_process_path(normalized_path, path_cache, now_ts):
                skipped += 1
                self._record_recent_event("SHARE-SKIP", normalized_path, "仍处于去重冷却中")
                continue
            if self._enqueue_path(normalized_path):
                enqueued += 1
                path_cache[normalized_path] = now_ts
            else:
                skipped += 1

        runtime_state["path_cache"] = self._trim_path_cache(path_cache, now_ts)
        self._save_runtime_state(runtime_state)
        self._record_recent_event(
            "SHARE-DONE",
            parent_path,
            f"分享转存新增项处理完成 新增={len(new_paths)} 入队={enqueued} 跳过={skipped}",
        )

    def _snapshot_share_parent(self, parent_path: str) -> Optional[Dict[str, str]]:
        """
        读取分享转存父目录的一层子项快照。
        返回 source-path 风格路径到签名的映射；失败返回 None。
        """
        if not self._clouddrive2_enabled:
            return self._snapshot_local_parent(parent_path)

        parent_item = self._build_clouddrive2_file_item(parent_path)
        if not parent_item:
            logger.warning("【115整理桥接】分享转存父目录无法解析: %s", parent_path)
            return None

        storagechain = self._storagechain or StorageChain()
        try:
            entries = storagechain.list_files(parent_item) or []
        except Exception as err:
            logger.error(f"【115整理桥接】分享转存目录 list_files 失败: {parent_path} - {err}", exc_info=True)
            return None

        snapshot: Dict[str, str] = {}
        normalized_parent = self._normalize_path(parent_path)
        for entry in entries:
            child_path = (Path(normalized_parent) / str(entry.name or "")).as_posix()
            if not entry.name:
                entry_path = str(getattr(entry, "path", "") or "")
                child_path = self._cloud_path_to_source_path(entry_path) or child_path
            snapshot[self._normalize_path(child_path)] = self._file_item_signature(entry)
        return snapshot

    def _snapshot_local_parent(self, parent_path: str) -> Optional[Dict[str, str]]:
        """
        本地路径模式的一层子项快照。
        """
        path_obj = Path(parent_path)
        if not path_obj.exists() or not path_obj.is_dir():
            return None
        snapshot: Dict[str, str] = {}
        try:
            for child in path_obj.iterdir():
                stat_result = child.stat()
                snapshot[self._normalize_path(child)] = f"{child.name}|{'dir' if child.is_dir() else 'file'}|{stat_result.st_size}|{stat_result.st_mtime}"
        except Exception as err:
            logger.error(f"【115整理桥接】本地分享转存目录快照失败: {parent_path} - {err}", exc_info=True)
            return None
        return snapshot

    def _cloud_path_to_source_path(self, cloud_path: str) -> str:
        """
        将 CloudDrive2 路径还原为115网盘源路径。
        """
        normalized_cloud_path = self._normalize_path(cloud_path)
        normalized_prefix = self._normalize_path(self._clouddrive2_prefix)
        if not normalized_cloud_path or not normalized_prefix:
            return ""
        if normalized_cloud_path == normalized_prefix:
            return "/"
        prefix_with_slash = normalized_prefix.rstrip("/") + "/"
        if normalized_cloud_path.startswith(prefix_with_slash):
            return "/" + normalized_cloud_path[len(prefix_with_slash):].lstrip("/")
        return normalized_cloud_path

    @staticmethod
    def _file_item_signature(file_item: FileItem) -> str:
        """
        构造轻量快照签名。
        """
        return "|".join(
            [
                str(getattr(file_item, "name", "") or ""),
                str(getattr(file_item, "type", "") or ""),
                str(getattr(file_item, "size", "") or ""),
                str(getattr(file_item, "modify_time", "") or ""),
            ]
        )

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
                "time": str(int(time())),
                "status": status,
                "path": path,
                "message": message,
            },
        )
        self.save_data(self.RECENT_EVENTS_KEY, recent_events[: self.RECENT_EVENTS_LIMIT])

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
