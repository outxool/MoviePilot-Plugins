import datetime
from typing import Any, List, Dict, Tuple, Optional
from pathlib import Path

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# MoviePilot v2.8.8-1 核心依赖（适配你的版本）
from app.chain.media import MediaChain
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.schemas.types import MediaType
from app.utils.http import RequestUtils


class TmdbTrendsSubscribe(_PluginBase):
    """
    TMDB趋势自动订阅插件
    适配：MoviePilot v2.8.8-1
    路径：plugins.v2/tmdbtrendssubscribe/__init__.py
    """
    # ========== v2插件基础信息（固定规范） ==========
    plugin_name = "TMDB趋势自动订阅"
    plugin_desc = "自动订阅TMDB趋势电影/电视剧/动画，支持自定义评分、订阅时间、分类及条数"
    plugin_icon = "tmdb.png"  # 对应assets目录下的图标
    plugin_version = "1.0.0"
    plugin_author = "YourName"
    author_url = "https://github.com/yourusername/tmdbtrendssubscribe"
    plugin_config_prefix = "tmdbtrendssubscribe_"
    plugin_order = 20
    auth_level = 2

    # ========== 插件核心配置项 ==========
    _enabled: bool = False
    _cron: str = "0 0 * * *"  # Cron表达式，默认每天凌晨执行
    _min_rating: float = 7.0  # 最低订阅评分阈值
    _notify: bool = True      # 订阅后是否发送通知
    _only_once: bool = False  # 是否立即运行一次
    # TMDB全部分类（可单独配置启用/禁用、订阅条数）
    _categories: dict = {
        "movie_popular": {"name": "热门电影", "enabled": True, "count": 5},
        "movie_top_rated": {"name": "高分电影", "enabled": True, "count": 5},
        "movie_upcoming": {"name": "即将上映电影", "enabled": True, "count": 5},
        "movie_now_playing": {"name": "正在上映电影", "enabled": True, "count": 5},
        "tv_popular": {"name": "热门电视剧", "enabled": True, "count": 5},
        "tv_top_rated": {"name": "高分电视剧", "enabled": True, "count": 5},
        "tv_on_the_air": {"name": "正在播出电视剧", "enabled": True, "count": 5},
        "tv_airing_today": {"name": "今日播出电视剧", "enabled": True, "count": 5},
        "tv_animation": {"name": "热门动画", "enabled": True, "count": 5}
    }

    # ========== 私有属性 ==========
    _scheduler: Optional[BackgroundScheduler] = None  # 定时器
    _cache_path: Optional[Path] = None                # 缓存路径
    _processed_ids: set = set()                       # 已处理媒体ID（防重复订阅）

    def init_plugin(self, config: dict = None):
        """初始化插件（v2插件必须实现）"""
        # 1. 初始化缓存目录
        self._cache_path = settings.TEMP_PATH / "tmdb_trends_cache"
        self._cache_path.mkdir(parents=True, exist_ok=True)
        self._load_processed_ids()

        # 2. 停止现有定时任务
        self.stop_service()

        # 3. 加载配置
        if config:
            self._enabled = config.get("enabled", False)
            self._cron = config.get("cron", self._cron)
            self._min_rating = float(config.get("min_rating", self._min_rating))
            self._notify = config.get("notify", self._notify)
            self._only_once = config.get("only_once", self._only_once)
            # 更新分类配置
            for cat_key, cat_config in config.get("categories", {}).items():
                if cat_key in self._categories:
                    self._categories[cat_key].update(cat_config)

        # 4. 启动任务
        if self._enabled:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            # 立即运行一次
            if self._only_once:
                self._scheduler.add_job(
                    self.check_and_subscribe,
                    'date',
                    run_date=datetime.datetime.now(tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=5),
                    name="TMDB趋势立即订阅"
                )
                # 重置立即运行标记
                self._only_once = False
                self.update_config({**config, "only_once": False})
            # 定时任务
            self._scheduler.add_job(
                self.check_and_subscribe,
                CronTrigger.from_crontab(self._cron),
                name="TMDB趋势定时订阅"
            )
            self._scheduler.start()
            logger.info(f"TMDB趋势订阅插件初始化完成（路径：plugins.v2/tmdbtrendssubscribe），执行周期：{self._cron}")

    def _load_processed_ids(self):
        """加载已处理的媒体ID，避免重复订阅"""
        cache_file = self._cache_path / "processed_ids.txt"
        if cache_file.exists():
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    self._processed_ids = set(line.strip() for line in f.readlines() if line.strip())
            except Exception as e:
                logger.error(f"加载已处理ID缓存失败：{str(e)}")

    def _save_processed_id(self, media_id: str):
        """保存已处理的媒体ID"""
        if media_id in self._processed_ids:
            return
        self._processed_ids.add(media_id)
        cache_file = self._cache_path / "processed_ids.txt"
        try:
            with open(cache_file, "a", encoding="utf-8") as f:
                f.write(f"{media_id}\n")
        except Exception as e:
            logger.error(f"保存已处理ID失败：{str(e)}")

    def _get_tmdb_data(self, media_type: str, trend_type: str, limit: int) -> List[dict]:
        """调用TMDB API获取趋势数据"""
        # 校验TMDB API密钥
        tmdb_api_key = settings.TMDB_API_KEY
        if not tmdb_api_key:
            logger.error("未配置TMDB API密钥！请在MoviePilot设置→媒体设置中配置")
            return []

        # 动画分类特殊处理（TMDB动画属于TV的16号类型）
        if trend_type == "animation":
            url = "https://api.themoviedb.org/3/discover/tv"
            params = {
                "api_key": tmdb_api_key,
                "language": "zh-CN",
                "with_genres": "16",  # 动画类型ID
                "sort_by": "popularity.desc",
                "page": 1
            }
        else:
            url = f"https://api.themoviedb.org/3/{media_type}/{trend_type}"
            params = {
                "api_key": tmdb_api_key,
                "language": "zh-CN",
                "page": 1
            }

        # 发送请求（适配MoviePilot代理配置）
        try:
            res = RequestUtils(proxies=settings.PROXY).get(url, params=params)
            if res and res.status_code == 200:
                return res.json().get("results", [])[:limit]
            logger.error(f"获取TMDB数据失败：{media_type}/{trend_type}，状态码：{res.status_code if res else '无响应'}")
            return []
        except Exception as e:
            logger.error(f"TMDB API请求异常：{str(e)}")
            return []

    def check_and_subscribe(self):
        """核心逻辑：检查并订阅符合条件的TMDB内容"""
        logger.info("===== 开始执行TMDB趋势订阅任务 =====")
        media_chain = MediaChain()
        subscribe_chain = SubscribeChain()

        # 遍历所有启用的分类
        for cat_key, cat_info in self._categories.items():
            if not cat_info.get("enabled", False):
                continue
            count = int(cat_info.get("count", 5))
            if count <= 0:
                continue
            logger.info(f"处理分类：{cat_info['name']}，计划订阅前{count}条")

            # 解析媒体类型和趋势类型
            if cat_key.startswith("movie"):
                media_type = "movie"
                trend_type = cat_key.replace("movie_", "")
            elif cat_key.startswith("tv"):
                media_type = "tv"
                trend_type = cat_key.replace("tv_", "")
            else:
                logger.warning(f"未知分类：{cat_key}，跳过")
                continue

            # 获取TMDB数据
            items = self._get_tmdb_data(media_type, trend_type, count)
            if not items:
                logger.info(f"分类「{cat_info['name']}」未获取到数据")
                continue

            # 处理每条数据
            for item in items:
                # 1. 检查评分
                rating = item.get("vote_average", 0)
                if rating < self._min_rating:
                    logger.info(f"{item.get('title') or item.get('name')} 评分{rating} < {self._min_rating}，跳过")
                    continue

                # 2. 生成唯一ID（防重复）
                media_id = f"{media_type}_{item.get('id')}"
                if media_id in self._processed_ids:
                    logger.debug(f"{item.get('title') or item.get('name')} 已处理过，跳过")
                    continue

                # 3. 识别媒体信息（MoviePilot v2规范）
                title = item.get("title") or item.get("name")
                release_date = item.get("release_date") or item.get("first_air_date")
                year = release_date.split("-")[0] if release_date else ""
                tmdb_id = item.get("id")

                mediainfo = media_chain.recognize_media(
                    meta={"title": title, "year": year, "tmdb_id": tmdb_id},
                    mtype=MediaType.MOVIE if media_type == "movie" else MediaType.TV,
                    cache=False
                )
                if not mediainfo:
                    logger.error(f"无法识别媒体：{title}（TMDB ID：{tmdb_id}）")
                    continue

                # 4. 检查是否已订阅
                if subscribe_chain.exists(tmdbid=mediainfo.tmdb_id, season=mediainfo.season):
                    logger.info(f"{mediainfo.title_year} 已订阅，跳过")
                    self._save_processed_id(media_id)
                    continue

                # 5. 执行订阅（适配v2.8.8-1版本）
                sid, msg = subscribe_chain.add(
                    title=mediainfo.title,
                    year=mediainfo.year,
                    mtype=mediainfo.type,
                    tmdbid=mediainfo.tmdb_id,
                    season=mediainfo.season,
                    exist_ok=True,
                    username="TMDB趋势订阅"
                )

                # 6. 订阅结果处理
                if sid:
                    logger.info(f"✅ 成功订阅：{mediainfo.title_year}（订阅ID：{sid}）")
                    self._save_processed_id(media_id)
                    # 发送通知
                    if self._notify:
                        self.post_message(
                            mtype=NotificationType.SiteMessage,
                            title=f"【TMDB趋势订阅】{cat_info['name']}",
                            text=f"✅ 成功订阅 {mediainfo.title_year}\n⭐ 评分：{rating}\n📌 类型：{mediainfo.type.value}\n🆔 TMDB ID：{tmdb_id}"
                        )
                else:
                    logger.error(f"❌ 订阅失败：{mediainfo.title_year}，原因：{msg}")

        logger.info("===== TMDB趋势订阅任务执行完成 =====")

    # ========== v2插件必须实现的接口 ==========
    def get_state(self) -> bool:
        """获取插件启用状态"""
        return self._enabled

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """生成前端配置表单（适配MoviePilot v2.8.8前端）"""
        # 构建分类配置项
        category_fields = []
        for cat_key, cat_info in self._categories.items():
            category_fields.append({
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {'cols': 12, 'md': 4},
                        'content': [
                            {
                                'component': 'VSwitch',
                                'props': {
                                    'model': f'categories.{cat_key}.enabled',
                                    'label': cat_info['name'],
                                    'value': cat_info['enabled']
                                }
                            }
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {'cols': 12, 'md': 8},
                        'content': [
                            {
                                'component': 'VTextField',
                                'props': {
                                    'model': f'categories.{cat_key}.count',
                                    'label': '订阅条数',
                                    'type': 'number',
                                    'min': 1,
                                    'max': 20,
                                    'value': cat_info['count']
                                }
                            }
                        ]
                    }
                ]
            })

        # 主表单
        form = [
            {
                'component': 'VForm',
                'content': [
                    # 基础开关配置
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                            'value': self._enabled
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '发送通知',
                                            'value': self._notify
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'only_once',
                                            'label': '立即运行一次',
                                            'value': self._only_once
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # 执行周期和最低评分
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 6},
                                'content': [
                                    {
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期（Cron）',
                                            'value': self._cron,
                                            'placeholder': '5位Cron表达式，例如 0 0 * * * 每天凌晨执行'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'min_rating',
                                            'label': '最低订阅评分',
                                            'type': 'number',
                                            'min': 0,
                                            'max': 10,
                                            'step': 0.1,
                                            'value': self._min_rating
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # 分类配置卡片
                    {
                        'component': 'VCard',
                        'props': {
                            'title': '分类订阅配置',
                            'variant': 'outlined',
                            'class': 'mt-4'
                        },
                        'content': category_fields
                    },
                    # 提示信息
                    {
                        'component': 'VAlert',
                        'props': {
                            'type': 'info',
                            'variant': 'tonal',
                            'class': 'mt-4',
                            'text': '⚠️ 注意：需先在MoviePilot「设置→媒体设置」中配置有效的TMDB API密钥，否则插件无法工作。'
                        }
                    }
                ]
            }
        ]

        # 表单默认值
        form_default = {
            "enabled": self._enabled,
            "cron": self._cron,
            "min_rating": self._min_rating,
            "notify": self._notify,
            "only_once": self._only_once,
            "categories": self._categories
        }

        return form, form_default

    def get_service(self) -> List[Dict[str, Any]]:
        """返回插件的定时服务（v2规范）"""
        if self._enabled and self._cron:
            return [
                {
                    "id": "TmdbTrendsSubscribe",
                    "name": "TMDB趋势订阅服务",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.check_and_subscribe,
                    "kwargs": {}
                }
            ]
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """自定义API接口（清除缓存）"""
        return [
            {
                "path": "/clear_cache",
                "endpoint": self.clear_cache,
                "methods": ["GET"],
                "summary": "清除已处理ID缓存"
            }
        ]

    def clear_cache(self):
        """清除已处理ID缓存，重置订阅记录"""
        try:
            cache_file = self._cache_path / "processed_ids.txt"
            if cache_file.exists():
                cache_file.unlink()
            self._processed_ids = set()
            return {"status": "success", "message": "已清除所有已处理ID缓存，可重新订阅历史内容"}
        except Exception as e:
            return {"status": "error", "message": f"清除缓存失败：{str(e)}"}

    def stop_service(self):
        """停止插件的定时服务（v2规范）"""
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown()
            self._scheduler = None
        logger.info("TMDB趋势订阅插件服务已停止")

    def get_command(self) -> List[Dict[str, Any]]:
        """自定义命令（暂无）"""
        return []