# 强制打印日志
print("加载 DoubanRank 插件模块 (v3.1.2)...")

import datetime
import json
import re
import time
import random
from threading import Event, Thread
from typing import Tuple, List, Dict, Any

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo
from app.log import logger
from app.plugins import _PluginBase
from app.utils.http import RequestUtils

# 兼容性导入
try:
    from app.schemas import MediaType, NotificationType
except ImportError:
    from app.schemas.types import MediaType, NotificationType

class DoubanRank(_PluginBase):
    # 插件基本信息
    plugin_name = "豆瓣榜单订阅增强版（自用）"
    plugin_desc = "直接抓取豆瓣官网数据，支持电影/剧集/综艺分类订阅，支持多榜单、评分年份过滤及智能去重。"
    plugin_icon = "https://img3.doubanio.com/favicon.ico"
    plugin_version = "3.1.2"
    plugin_author = "outxool"
    plugin_config_prefix = "doubanrank_"
    plugin_order = 6
    auth_level = 2

    _event = Event()
    _scheduler = None
    
    # 运行时的链对象
    subscribechain: SubscribeChain = None
    downloadchain: DownloadChain = None
    mediachain: MediaChain = None
    
    # -----------------------
    # 榜单定义
    # -----------------------
    # 电影
    _movie_ranks_conf = {
        'movie_hot': {'name': '热门电影', 'type': 'api', 'url': 'https://movie.douban.com/j/search_subjects?type=movie&tag=%E7%83%AD%E9%97%A8&sort=recommend&page_limit=50&page_start=0'},
        'movie_top250': {'name': '电影Top250', 'type': 'html', 'url': 'https://movie.douban.com/top250'},
        'movie_weekly': {'name': '一周口碑榜', 'type': 'html', 'url': 'https://movie.douban.com/chart'},
        'movie_new': {'name': '新片榜', 'type': 'html', 'url': 'https://movie.douban.com/chart'},
    }
    # 电视剧 (含动画)
    _tv_ranks_conf = {
        'tv_hot': {'name': '热门电视剧(综合)', 'type': 'api', 'url': 'https://movie.douban.com/j/search_subjects?type=tv&tag=%E7%83%AD%E9%97%A8&sort=recommend&page_limit=50&page_start=0'},
        'tv_domestic': {'name': '热门国产剧', 'type': 'api', 'url': 'https://movie.douban.com/j/search_subjects?type=tv&tag=%E5%9B%BD%E4%BA%A7%E5%89%A7&sort=recommend&page_limit=50&page_start=0'},
        'tv_american': {'name': '热门美剧', 'type': 'api', 'url': 'https://movie.douban.com/j/search_subjects?type=tv&tag=%E7%BE%8E%E5%89%A7&sort=recommend&page_limit=50&page_start=0'},
        'tv_japanese': {'name': '热门日剧', 'type': 'api', 'url': 'https://movie.douban.com/j/search_subjects?type=tv&tag=%E6%97%A5%E5%89%A7&sort=recommend&page_limit=50&page_start=0'},
        'tv_korean': {'name': '热门韩剧', 'type': 'api', 'url': 'https://movie.douban.com/j/search_subjects?type=tv&tag=%E9%9F%A9%E5%89%A7&sort=recommend&page_limit=50&page_start=0'},
        'tv_animation': {'name': '热门动画番剧', 'type': 'api', 'url': 'https://movie.douban.com/j/search_subjects?type=tv&tag=%E6%97%A5%E6%9C%AC%E5%8A%A8%E7%94%BB&sort=recommend&page_limit=50&page_start=0'},
        'tv_documentary': {'name': '热门纪录片', 'type': 'api', 'url': 'https://movie.douban.com/j/search_subjects?type=tv&tag=%E7%BA%AA%E5%BD%95%E7%89%87&sort=recommend&page_limit=50&page_start=0'},
    }
    # 综艺
    _show_ranks_conf = {
        'show_hot': {'name': '热门综艺(综合)', 'type': 'api', 'url': 'https://movie.douban.com/j/search_subjects?type=tv&tag=%E7%BB%BC%E8%89%BA&sort=recommend&page_limit=50&page_start=0'},
        'show_domestic': {'name': '国内综艺', 'type': 'api', 'url': 'https://movie.douban.com/j/search_subjects?type=tv&tag=%E5%9B%BD%E5%86%85%E7%BB%BC%E8%89%BA&sort=recommend&page_limit=50&page_start=0'},
        'show_foreign': {'name': '国外综艺', 'type': 'api', 'url': 'https://movie.douban.com/j/search_subjects?type=tv&tag=%E5%9B%BD%E5%A4%96%E7%BB%BC%E8%89%BA&sort=recommend&page_limit=50&page_start=0'},
    }

    # -----------------------
    # 配置属性
    # -----------------------
    _enabled = False
    _cron = "0 10 * * *"
    _proxy = False
    _notify = True
    _onlyonce = False
    _clear_history = False
    
    # 电影配置
    _movie_enabled = False
    _movie_ranks = []
    _movie_min_vote = 7.0
    _movie_min_year = 0
    _movie_count = 10
    
    # 电视剧配置
    _tv_enabled = False
    _tv_ranks = []
    _tv_min_vote = 7.5
    _tv_min_year = 0
    _tv_count = 10
    
    # 综艺配置
    _show_enabled = False
    _show_ranks = []
    _show_min_vote = 7.0
    _show_min_year = 0
    _show_count = 10

    def init_plugin(self, config: dict = None):
        logger.info("正在初始化豆瓣榜单订阅插件 (v3.1.2)...")
        self.subscribechain = SubscribeChain()
        self.downloadchain = DownloadChain()
        self.mediachain = MediaChain()

        if config:
            self._enabled = config.get("enabled", False)
            self._cron = config.get("cron", "0 10 * * *")
            self._proxy = config.get("proxy", False)
            self._notify = config.get("notify", True)
            self._onlyonce = config.get("onlyonce", False)
            self._clear_history = config.get("clear_history", False)
            
            # Movie
            self._movie_enabled = config.get("movie_enabled", False)
            self._movie_ranks = config.get("movie_ranks", [])
            self._movie_min_vote = float(config.get("movie_min_vote", 7.0))
            self._movie_min_year = int(config.get("movie_min_year", 0))
            self._movie_count = int(config.get("movie_count", 10))
            
            # TV
            self._tv_enabled = config.get("tv_enabled", False)
            self._tv_ranks = config.get("tv_ranks", [])
            self._tv_min_vote = float(config.get("tv_min_vote", 7.5))
            self._tv_min_year = int(config.get("tv_min_year", 0))
            self._tv_count = int(config.get("tv_count", 10))
            
            # Variety
            self._show_enabled = config.get("show_enabled", False)
            self._show_ranks = config.get("show_ranks", [])
            self._show_min_vote = float(config.get("show_min_vote", 7.0))
            self._show_min_year = int(config.get("show_min_year", 0))
            self._show_count = int(config.get("show_count", 10))

        self.stop_service()

        if self._enabled or self._onlyonce:
            if self._enabled and self._cron:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._scheduler.add_job(func=self.refresh_douban, trigger=CronTrigger.from_crontab(self._cron), name="豆瓣榜单订阅")
                if self._scheduler.get_jobs():
                    self._scheduler.start()

            self.__execute_once_operations()

    def __execute_once_operations(self):
        config_updated = False
        
        if self._clear_history:
            self.save_data('history', [])
            self._clear_history = False
            config_updated = True
            logger.info("豆瓣榜单订阅：历史记录已清理")

        if self._onlyonce:
            logger.info("豆瓣榜单订阅：检测到立即运行指令，正在后台执行...")
            Thread(target=self.refresh_douban).start()
            self._onlyonce = False
            config_updated = True

        if config_updated:
            self.__update_config()

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "cron": self._cron,
            "proxy": self._proxy,
            "notify": self._notify,
            "onlyonce": self._onlyonce,
            "clear_history": self._clear_history,
            
            "movie_enabled": self._movie_enabled,
            "movie_ranks": self._movie_ranks,
            "movie_min_vote": self._movie_min_vote,
            "movie_min_year": self._movie_min_year,
            "movie_count": self._movie_count,
            
            "tv_enabled": self._tv_enabled,
            "tv_ranks": self._tv_ranks,
            "tv_min_vote": self._tv_min_vote,
            "tv_min_year": self._tv_min_year,
            "tv_count": self._tv_count,
            
            "show_enabled": self._show_enabled,
            "show_ranks": self._show_ranks,
            "show_min_vote": self._show_min_vote,
            "show_min_year": self._show_min_year,
            "show_count": self._show_count,
        })

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {"path": "/delete_history", "endpoint": self.delete_history, "methods": ["GET"], "summary": "删除历史"}
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            return [{"id": "DoubanRank", "name": "豆瓣榜单订阅服务", "trigger": CronTrigger.from_crontab(self._cron), "func": self.refresh_douban, "kwargs": {}}]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        # 选项定义
        movie_opts = [{'title': v['name'], 'value': k} for k, v in self._movie_ranks_conf.items()]
        tv_opts = [{'title': v['name'], 'value': k} for k, v in self._tv_ranks_conf.items()]
        show_opts = [{'title': v['name'], 'value': k} for k, v in self._show_ranks_conf.items()]

        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'proxy', 'label': '使用代理服务器'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'notify', 'label': '发送通知'}}]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VCronField', 'props': {'model': 'cron', 'label': '执行周期', 'placeholder': '5位cron表达式'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VTextField', 'props': {'model': 'vote', 'label': '最低评分(旧版兼容)', 'placeholder': '不再使用，请在下方分项配置'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '立即运行一次'}}]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'content': [{'component': 'VSelect', 'props': {'chips': True, 'multiple': True, 'model': 'ranks', 'label': '旧版榜单(不再使用)', 'items': []}}]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VSwitch', 'props': {'model': 'clear_history', 'label': '清理历史记录'}}]}
                        ]
                    },
                    # 电影配置
                    {'component': 'VAlert', 'props': {'type': 'info', 'text': '电影榜单配置', 'variant': 'tonal', 'class': 'mt-4'}},
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 2}, 'content': [{'component': 'VSwitch', 'props': {'model': 'movie_enabled', 'label': '启用'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSelect', 'props': {'model': 'movie_ranks', 'label': '选择榜单(多选)', 'multiple': True, 'chips': True, 'items': movie_opts}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 2}, 'content': [{'component': 'VTextField', 'props': {'model': 'movie_min_vote', 'label': '最低分', 'type': 'number'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 2}, 'content': [{'component': 'VTextField', 'props': {'model': 'movie_min_year', 'label': '最低年份', 'placeholder': '0为不限'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 2}, 'content': [{'component': 'VTextField', 'props': {'model': 'movie_count', 'label': 'Top N', 'type': 'number'}}]}
                        ]
                    },
                    # 电视剧配置
                    {'component': 'VAlert', 'props': {'type': 'success', 'text': '电视剧订阅配置', 'variant': 'tonal', 'class': 'mt-4'}},
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 2}, 'content': [{'component': 'VSwitch', 'props': {'model': 'tv_enabled', 'label': '启用'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSelect', 'props': {'model': 'tv_ranks', 'label': '选择榜单(多选)', 'multiple': True, 'chips': True, 'items': tv_opts}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 2}, 'content': [{'component': 'VTextField', 'props': {'model': 'tv_min_vote', 'label': '最低分', 'type': 'number'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 2}, 'content': [{'component': 'VTextField', 'props': {'model': 'tv_min_year', 'label': '最低年份', 'placeholder': '0为不限'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 2}, 'content': [{'component': 'VTextField', 'props': {'model': 'tv_count', 'label': 'Top N', 'type': 'number'}}]}
                        ]
                    },
                    # 综艺配置
                    {'component': 'VAlert', 'props': {'type': 'warning', 'text': '综艺订阅配置', 'variant': 'tonal', 'class': 'mt-4'}},
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 2}, 'content': [{'component': 'VSwitch', 'props': {'model': 'show_enabled', 'label': '启用'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSelect', 'props': {'model': 'show_ranks', 'label': '选择榜单(多选)', 'multiple': True, 'chips': True, 'items': show_opts}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 2}, 'content': [{'component': 'VTextField', 'props': {'model': 'show_min_vote', 'label': '最低分', 'type': 'number'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 2}, 'content': [{'component': 'VTextField', 'props': {'model': 'show_min_year', 'label': '最低年份', 'placeholder': '0为不限'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 2}, 'content': [{'component': 'VTextField', 'props': {'model': 'show_count', 'label': 'Top N', 'type': 'number'}}]}
                        ]
                    }
                ]
            }
        ], {
            "enabled": False, "cron": "0 10 * * *", "proxy": False, "notify": True, "onlyonce": False, "clear_history": False,
            "movie_enabled": False, "movie_ranks": [], "movie_min_vote": 7.0, "movie_min_year": 0, "movie_count": 10,
            "tv_enabled": False, "tv_ranks": [], "tv_min_vote": 7.5, "tv_min_year": 0, "tv_count": 10,
            "show_enabled": False, "show_ranks": [], "show_min_vote": 7.0, "show_min_year": 0, "show_count": 10,
        }

    def get_page(self) -> List[dict]:
        historys = self.get_data('history')
        if not historys:
            return [{'component': 'div', 'text': '暂无数据', 'props': {'class': 'text-center'}}]
        
        historys = sorted(historys, key=lambda x: x.get('time'), reverse=True)[:50]
        contents = []
        for history in historys:
            title = history.get("title")
            doubanid = history.get("doubanid")
            contents.append({
                'component': 'VCard',
                'props': {'class': 'mx-auto mb-2', 'width': '100%'},
                'content': [
                    {
                        "component": "VDialogCloseBtn",
                        "props": {'innerClass': 'absolute top-0 right-0'},
                        'events': {
                            'click': {
                                'api': 'plugin/DoubanRank/delete_history',
                                'method': 'get',
                                'params': {'key': f"doubanrank: {title} (DB:{doubanid})", 'apikey': settings.API_TOKEN}
                            }
                        },
                    },
                    {
                        'component': 'div',
                        'props': {'class': 'd-flex justify-space-start flex-nowrap flex-row'},
                        'content': [
                            {'component': 'div', 'content': [{'component': 'VImg', 'props': {'src': history.get("poster"), 'height': 120, 'width': 80, 'aspect-ratio': '2/3', 'class': 'object-cover shadow ring-gray-500', 'cover': True}}]},
                            {'component': 'div', 'content': [
                                {'component': 'VCardTitle', 'props': {'class': 'ps-1 pe-5 break-words whitespace-break-spaces'}, 'content': [{'component': 'a', 'props': {'href': f"https://movie.douban.com/subject/{doubanid}", 'target': '_blank'}, 'text': title}]},
                                {'component': 'VCardText', 'props': {'class': 'pa-0 px-2'}, 'text': f'类型：{history.get("type")} | {history.get("year")}'},
                                {'component': 'VCardText', 'props': {'class': 'pa-0 px-2'}, 'text': f'评分：{history.get("vote")} | {history.get("rank_type")}'},
                                {'component': 'VCardText', 'props': {'class': 'pa-0 px-2'}, 'text': f'时间：{history.get("time")}'}
                            ]}
                        ]
                    }
                ]
            })
        return [{'component': 'div', 'props': {'class': 'grid gap-3 grid-info-card'}, 'content': contents}]

    def stop_service(self):
        pass

    def delete_history(self, key: str, apikey: str):
        if apikey != settings.API_TOKEN:
            return {"success": False, "message": "API密钥错误"}
        historys = self.get_data('history') or []
        historys = [h for h in historys if h.get("unique") != key]
        self.save_data('history', historys)
        return {"success": True, "message": "删除成功"}

    def refresh_douban(self):
        """主任务逻辑"""
        logger.info(f"开始执行豆瓣榜单订阅任务...")
        
        tasks = []
        if self._movie_enabled and self._movie_ranks:
            tasks.append({
                'ranks': self._movie_ranks, 'config_map': self._movie_ranks_conf,
                'min_vote': self._movie_min_vote, 'min_year': self._movie_min_year, 'limit': self._movie_count, 'cat': '电影'
            })
        if self._tv_enabled and self._tv_ranks:
            tasks.append({
                'ranks': self._tv_ranks, 'config_map': self._tv_ranks_conf,
                'min_vote': self._tv_min_vote, 'min_year': self._tv_min_year, 'limit': self._tv_count, 'cat': '电视剧'
            })
        if self._show_enabled and self._show_ranks:
            tasks.append({
                'ranks': self._show_ranks, 'config_map': self._show_ranks_conf,
                'min_vote': self._show_min_vote, 'min_year': self._show_min_year, 'limit': self._show_count, 'cat': '综艺'
            })

        if not tasks:
            logger.info("未启用任何订阅配置")
            return

        added_list = []
        history = self.get_data('history') or []

        for task in tasks:
            config_map = task['config_map']
            limit = task['limit']
            
            for rank_key in task['ranks']:
                rank_conf = config_map.get(rank_key)
                if not rank_conf: continue
                
                logger.info(f"正在获取榜单：{rank_conf['name']}")
                
                try:
                    items = self.__get_douban_data(rank_conf)
                    if not items:
                        logger.warning(f"榜单 {rank_conf['name']} 未获取到数据")
                        continue
                    
                    process_items = items[:limit]
                    logger.info(f"榜单 {rank_conf['name']} 获取到 {len(items)} 条，将处理前 {len(process_items)} 条")
                    
                    for item in process_items:
                        if self._event.is_set(): return
                        
                        title = item.get('title')
                        douban_id = item.get('id')
                        try:
                            vote = float(item.get('rate') or 0)
                        except ValueError:
                            vote = 0.0
                            
                        year = item.get('year')
                        
                        # 1. 评分过滤
                        if task['min_vote'] > 0 and vote < task['min_vote']: 
                            logger.info(f"跳过 {title}: 评分 {vote} 低于 {task['min_vote']}")
                            continue
                        
                        # 2. 年份过滤
                        if year and task['min_year'] > 0:
                            try:
                                year_int = int(re.findall(r'\d{4}', str(year))[0])
                                if year_int < task['min_year']: 
                                    logger.info(f"跳过 {title}: 年份 {year_int} 早于 {task['min_year']}")
                                    continue
                            except (ValueError, IndexError): pass

                        # 3. 插件历史去重
                        unique_flag = f"doubanrank: {title} (DB:{douban_id})"
                        if any(h.get('unique') == unique_flag for h in history): 
                            logger.info(f"跳过 {title}: 历史记录中已存在")
                            continue
                        
                        # 4. 识别与入库
                        meta = MetaInfo(title)
                        if year: meta.year = str(year)
                        meta.type = MediaType.MOVIE if task['cat'] == '电影' else MediaType.TV
                        
                        mediainfo = self.__recognize_media(meta, douban_id)
                        if not mediainfo:
                            logger.warn(f'未识别到媒体信息: {title}')
                            continue
                        
                        # 5. 精确年份过滤 (识别后)
                        if task['min_year'] > 0 and mediainfo.year:
                            try:
                                if int(mediainfo.year) < task['min_year']: 
                                    logger.info(f"跳过 {title}: 识别后年份 {mediainfo.year} 早于 {task['min_year']}")
                                    continue
                            except: pass

                        # 6. 核心去重
                        if self.__check_exists(mediainfo, meta): 
                            logger.info(f"跳过 {title}: 媒体库或订阅列表中已存在")
                            continue
                        
                        # 7. 添加订阅
                        if self.__add_subscribe(mediainfo, meta, douban_id, rank_conf['name']):
                            added_list.append({'title': title, 'type': rank_conf['name'], 'vote': vote})
                            
                            history.append({
                                "title": title, "type": mediainfo.type.value, "year": mediainfo.year,
                                "poster": mediainfo.get_poster_image(), "overview": mediainfo.overview,
                                "tmdbid": mediainfo.tmdb_id, "doubanid": douban_id, "vote": vote,
                                "rank_type": rank_conf['name'],
                                "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "unique": unique_flag
                            })
                            self.save_data('history', history[-500:])
                        
                        time.sleep(random.uniform(1, 2))
                        
                except Exception as e:
                    logger.error(f"处理榜单 {rank_conf['name']} 出错: {e}")

        if self._notify and added_list:
            self.__send_notification(added_list)
            
        logger.info(f"所有豆瓣榜单处理完成")

    def __recognize_media(self, meta: MetaInfo, douban_id: str) -> MediaInfo:
        mediainfo = None
        if douban_id and settings.RECOGNIZE_SOURCE == "themoviedb":
            try:
                tmdbinfo = self.mediachain.get_tmdbinfo_by_doubanid(doubanid=douban_id, mtype=meta.type)
                if tmdbinfo:
                    mediainfo = self.chain.recognize_media(meta=meta, tmdbid=tmdbinfo.get("id"))
            except Exception: pass
        
        if not mediainfo:
            mediainfo = self.chain.recognize_media(meta=meta)
        return mediainfo

    def __check_exists(self, mediainfo: MediaInfo, meta: MetaInfo) -> bool:
        exist_flag, _ = self.downloadchain.get_no_exists_info(meta=meta, mediainfo=mediainfo)
        if exist_flag: return True
        if self.subscribechain.exists(mediainfo=mediainfo, meta=meta): return True
        return False

    def __add_subscribe(self, mediainfo: MediaInfo, meta: MetaInfo, douban_id: str, category_name: str) -> bool:
        try:
            self.subscribechain.add(
                title=mediainfo.title, year=mediainfo.year, mtype=mediainfo.type, 
                tmdbid=mediainfo.tmdb_id, season=meta.begin_season, exist_ok=True, username="豆瓣榜单"
            )
            logger.info(f"[{category_name}] 订阅成功: {mediainfo.title_year}")
            return True
        except Exception as e:
            logger.error(f"订阅失败: {e}")
            return False

    def __get_douban_data(self, rank_conf) -> List[dict]:
        url = rank_conf['url']
        rtype = rank_conf['type']
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://movie.douban.com/"
        }
        
        req_proxy = settings.PROXY if (self._proxy and settings.PROXY) else None
        req = RequestUtils(proxies=req_proxy) if req_proxy else RequestUtils()
            
        try:
            res = req.get_res(url, headers=headers)
            if not res or res.status_code != 200:
                logger.error(f"请求豆瓣失败: {url} (Code: {res.status_code if res else 'None'})")
                return []
            
            results = []
            
            if rtype == 'api':
                try:
                    data = res.json()
                    subjects = data.get('subjects', [])
                    for sub in subjects:
                        results.append({
                            'title': sub.get('title'),
                            'rate': sub.get('rate'),
                            'id': sub.get('id'),
                            'year': None
                        })
                except Exception: logger.error("API解析失败")
            
            elif rtype == 'html':
                html = res.text
                if 'movie_top250' in url:
                    pattern = re.compile(r'class="hd">\s*<a href=".*?/subject/(\d+)/".*?<span class="title">([^<]+)</span>.*?<span class="rating_num"[^>]*>([\d\.]+)</span>', re.S)
                    matches = pattern.findall(html)
                    for m in matches:
                        results.append({'id': m[0], 'title': m[1], 'rate': m[2], 'year': None})
                elif 'chart' in url:
                    pattern = re.compile(r'<a class="nbg" href=".*?/subject/(\d+)/"\s*title="([^"]+)".*?<span class="rating_nums">([\d\.]+)</span>', re.S)
                    matches = pattern.findall(html)
                    for m in matches:
                        results.append({'id': m[0], 'title': m[1], 'rate': m[2], 'year': None})
            
            return results
        except Exception as e:
            logger.error(f"解析数据失败: {e}")
            return []

    def __send_notification(self, items):
        if not items: return
        text = "\n".join([f"• [{i['type']}] {i['title']} ({i['vote']}分)" for i in items])
        self.post_message(mtype=NotificationType.Subscribe, title=f"豆瓣订阅新增 {len(items)} 部", text=text)
