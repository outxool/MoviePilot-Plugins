# 强制打印日志
print("加载 TmdbTrending 插件模块 (v1.2.0)...")

import datetime
from threading import Thread
from typing import Tuple, List, Dict, Any

from apscheduler.triggers.cron import CronTrigger

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

class TmdbTrending(_PluginBase):
    # 插件基本信息
    plugin_name = "TMDB趋势订阅"
    plugin_desc = "订阅 TMDB 趋势、热映、热门、高分及指定分类榜单，支持多榜单并发、年份过滤与去重。"
    plugin_icon = "https://www.themoviedb.org/assets/2/v4/logos/v2/blue_square_2-d537fb228cf3ded904ef09b136fe3fec72548ebc1fea3fbbd1ad9e36364db38b.svg"
    plugin_version = "1.2.0"
    plugin_author = "MoviePilot-Plugins"
    plugin_config_prefix = "tmdbtrending_"
    plugin_order = 10
    auth_level = 1

    # 私有属性
    subscribechain: SubscribeChain = None
    
    # 全局配置
    _enabled = False
    _cron = "0 10 * * *"
    _notify = True
    _onlyonce = False
    _clear_history = False
    _tmdb_api_key = ""
    
    # 电影配置
    _movie_enabled = False
    _movie_sources = ["trending_day"] # 改为列表
    _movie_genres = [] # 改为列表
    _movie_min_vote = 7.0
    _movie_min_year = 0
    _movie_count = 10
    
    # 电视剧配置
    _tv_enabled = False
    _tv_sources = ["trending_week"] # 改为列表
    _tv_genres = [] # 改为列表
    _tv_min_vote = 7.5
    _tv_min_year = 0
    _tv_count = 10
    
    # 动漫配置
    _anime_enabled = False
    _anime_window = "week"
    _anime_min_vote = 7.0
    _anime_min_year = 0
    _anime_count = 10

    def init_plugin(self, config: dict = None):
        logger.info("正在初始化 TMDB 趋势订阅插件...")
        self.subscribechain = SubscribeChain()
        
        if config:
            self._enabled = config.get("enabled", False)
            self._cron = config.get("cron", "0 10 * * *")
            self._notify = config.get("notify", True)
            self._onlyonce = config.get("onlyonce", False)
            self._clear_history = config.get("clear_history", False)
            self._tmdb_api_key = config.get("tmdb_api_key", "")
            
            # 电影
            self._movie_enabled = config.get("movie_enabled", False)
            self._movie_sources = config.get("movie_sources", ["trending_day"])
            self._movie_genres = config.get("movie_genres", [])
            self._movie_min_vote = float(config.get("movie_min_vote", 7.0))
            self._movie_min_year = int(config.get("movie_min_year", 0))
            self._movie_count = int(config.get("movie_count", 10))
            
            # 电视剧
            self._tv_enabled = config.get("tv_enabled", False)
            self._tv_sources = config.get("tv_sources", ["trending_week"])
            self._tv_genres = config.get("tv_genres", [])
            self._tv_min_vote = float(config.get("tv_min_vote", 7.5))
            self._tv_min_year = int(config.get("tv_min_year", 0))
            self._tv_count = int(config.get("tv_count", 10))
            
            # 动漫
            self._anime_enabled = config.get("anime_enabled", False)
            self._anime_window = config.get("anime_window", "week")
            self._anime_min_vote = float(config.get("anime_min_vote", 7.0))
            self._anime_min_year = int(config.get("anime_min_year", 0))
            self._anime_count = int(config.get("anime_count", 10))

        self.__execute_once_operations()

    def __execute_once_operations(self):
        config_updated = False
        update_dict = {}

        if self._clear_history:
            logger.info("TMDB趋势订阅：正在清除历史记录...")
            self.save_data('history', [])
            self._clear_history = False
            update_dict["clear_history"] = False
            config_updated = True
            logger.info("TMDB趋势订阅：历史记录已清除。")

        if self._onlyonce:
            logger.info("TMDB趋势订阅：检测到“立即运行”指令，正在后台启动任务...")
            Thread(target=self.sync_tmdb_trends).start()
            self._onlyonce = False
            update_dict["onlyonce"] = False
            config_updated = True
        
        if config_updated:
            self.update_config(update_dict)

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            return [{
                "id": "TmdbTrending",
                "name": "TMDB趋势订阅",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.sync_tmdb_trends,
                "kwargs": {}
            }]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        # 数据源选项
        movie_sources = [
            {'title': '今日趋势 (Trending Day)', 'value': 'trending_day'},
            {'title': '本周趋势 (Trending Week)', 'value': 'trending_week'},
            {'title': '正在热映 (Now Playing)', 'value': 'now_playing'},
            {'title': '热门电影 (Popular)', 'value': 'popular'},
            {'title': '高分电影 (Top Rated)', 'value': 'top_rated'},
            {'title': '按分类发现 (Discovery)', 'value': 'discover'},
        ]
        
        tv_sources = [
            {'title': '今日趋势 (Trending Day)', 'value': 'trending_day'},
            {'title': '本周趋势 (Trending Week)', 'value': 'trending_week'},
            {'title': '正在热播 (Airing Today)', 'value': 'airing_today'},
            {'title': '即将播出 (On The Air)', 'value': 'on_the_air'},
            {'title': '热门剧集 (Popular)', 'value': 'popular'},
            {'title': '高分剧集 (Top Rated)', 'value': 'top_rated'},
            {'title': '按分类发现 (Discovery)', 'value': 'discover'},
        ]

        # 电影分类
        movie_genres_opt = [
            {'title': '动作 (Action)', 'value': '28'},
            {'title': '冒险 (Adventure)', 'value': '12'},
            {'title': '动画 (Animation)', 'value': '16'},
            {'title': '喜剧 (Comedy)', 'value': '35'},
            {'title': '犯罪 (Crime)', 'value': '80'},
            {'title': '纪录 (Documentary)', 'value': '99'},
            {'title': '剧情 (Drama)', 'value': '18'},
            {'title': '家庭 (Family)', 'value': '10751'},
            {'title': '奇幻 (Fantasy)', 'value': '14'},
            {'title': '历史 (History)', 'value': '36'},
            {'title': '恐怖 (Horror)', 'value': '27'},
            {'title': '音乐 (Music)', 'value': '10402'},
            {'title': '悬疑 (Mystery)', 'value': '9648'},
            {'title': '爱情 (Romance)', 'value': '10749'},
            {'title': '科幻 (Sci-Fi)', 'value': '878'},
            {'title': '电视电影 (TV Movie)', 'value': '10770'},
            {'title': '惊悚 (Thriller)', 'value': '53'},
            {'title': '战争 (War)', 'value': '10752'},
            {'title': '西部 (Western)', 'value': '37'},
        ]

        # 电视剧分类
        tv_genres_opt = [
            {'title': '动作冒险 (Action & Adventure)', 'value': '10759'},
            {'title': '动画 (Animation)', 'value': '16'},
            {'title': '喜剧 (Comedy)', 'value': '35'},
            {'title': '犯罪 (Crime)', 'value': '80'},
            {'title': '纪录 (Documentary)', 'value': '99'},
            {'title': '剧情 (Drama)', 'value': '18'},
            {'title': '家庭 (Family)', 'value': '10751'},
            {'title': '儿童 (Kids)', 'value': '10762'},
            {'title': '悬疑 (Mystery)', 'value': '9648'},
            {'title': '新闻 (News)', 'value': '10763'},
            {'title': '真人秀 (Reality)', 'value': '10764'},
            {'title': '科幻奇幻 (Sci-Fi & Fantasy)', 'value': '10765'},
            {'title': '肥皂剧 (Soap)', 'value': '10766'},
            {'title': '脱口秀 (Talk)', 'value': '10767'},
            {'title': '战争政治 (War & Politics)', 'value': '10768'},
            {'title': '西部 (Western)', 'value': '37'},
        ]

        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [
                                {'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}
                            ]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [
                                {'component': 'VSwitch', 'props': {'model': 'notify', 'label': '发送通知'}}
                            ]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [
                                {'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '立即运行一次'}}
                            ]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [
                                {'component': 'VSwitch', 'props': {'model': 'clear_history', 'label': '清除历史记录'}}
                            ]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [
                                {'component': 'VCronField', 'props': {'model': 'cron', 'label': '执行周期'}}
                            ]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [
                                {'component': 'VTextField', 'props': {'model': 'tmdb_api_key', 'label': 'TMDB API Key', 'placeholder': '留空则使用系统默认'}}
                            ]}
                        ]
                    },
                    # 电影配置
                    {'component': 'VAlert', 'props': {'type': 'info', 'text': '电影订阅配置', 'variant': 'tonal', 'class': 'mt-4'}},
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 2}, 'content': [{'component': 'VSwitch', 'props': {'model': 'movie_enabled', 'label': '启用'}}]},
                            # 多选榜单
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSelect', 'props': {'model': 'movie_sources', 'label': '榜单来源(可多选)', 'multiple': True, 'chips': True, 'items': movie_sources}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 2}, 'content': [{'component': 'VTextField', 'props': {'model': 'movie_min_vote', 'label': '最低分', 'type': 'number'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 2}, 'content': [{'component': 'VTextField', 'props': {'model': 'movie_min_year', 'label': '最低年份', 'placeholder': '0为不限', 'type': 'number'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 2}, 'content': [{'component': 'VTextField', 'props': {'model': 'movie_count', 'label': '单榜数量', 'type': 'number'}}]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            # 多选分类
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 12}, 'content': [{'component': 'VSelect', 'props': {'model': 'movie_genres', 'label': '指定分类 (仅Discovery来源生效, 可多选)', 'multiple': True, 'chips': True, 'clearable': True, 'items': movie_genres_opt}}]}
                        ]
                    },
                    # 电视剧配置
                    {'component': 'VAlert', 'props': {'type': 'success', 'text': '电视剧订阅配置', 'variant': 'tonal', 'class': 'mt-4'}},
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 2}, 'content': [{'component': 'VSwitch', 'props': {'model': 'tv_enabled', 'label': '启用'}}]},
                            # 多选榜单
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSelect', 'props': {'model': 'tv_sources', 'label': '榜单来源(可多选)', 'multiple': True, 'chips': True, 'items': tv_sources}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 2}, 'content': [{'component': 'VTextField', 'props': {'model': 'tv_min_vote', 'label': '最低分', 'type': 'number'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 2}, 'content': [{'component': 'VTextField', 'props': {'model': 'tv_min_year', 'label': '最低年份', 'placeholder': '0为不限', 'type': 'number'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 2}, 'content': [{'component': 'VTextField', 'props': {'model': 'tv_count', 'label': '单榜数量', 'type': 'number'}}]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            # 多选分类
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 12}, 'content': [{'component': 'VSelect', 'props': {'model': 'tv_genres', 'label': '指定分类 (仅Discovery来源生效, 可多选)', 'multiple': True, 'chips': True, 'clearable': True, 'items': tv_genres_opt}}]}
                        ]
                    },
                    # 动漫配置
                    {'component': 'VAlert', 'props': {'type': 'warning', 'text': '动漫订阅配置 (独立预设：自动筛选日漫+动画)', 'variant': 'tonal', 'class': 'mt-4'}},
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 2}, 'content': [{'component': 'VSwitch', 'props': {'model': 'anime_enabled', 'label': '启用'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSelect', 'props': {'model': 'anime_window', 'label': '趋势周期', 'items': [{'title': '今日', 'value': 'day'}, {'title': '本周', 'value': 'week'}]}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 2}, 'content': [{'component': 'VTextField', 'props': {'model': 'anime_min_vote', 'label': '最低分', 'type': 'number'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 2}, 'content': [{'component': 'VTextField', 'props': {'model': 'anime_min_year', 'label': '最低年份', 'placeholder': '0为不限', 'type': 'number'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 2}, 'content': [{'component': 'VTextField', 'props': {'model': 'anime_count', 'label': '数量', 'type': 'number'}}]}
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "clear_history": False,
            "notify": True,
            "cron": "0 10 * * *",
            "tmdb_api_key": "",
            # Movie
            "movie_enabled": False,
            "movie_sources": ["trending_day"],
            "movie_genres": [],
            "movie_min_vote": 7.0,
            "movie_min_year": 0,
            "movie_count": 10,
            # TV
            "tv_enabled": False,
            "tv_sources": ["trending_week"],
            "tv_genres": [],
            "tv_min_vote": 7.5,
            "tv_min_year": 0,
            "tv_count": 10,
            # Anime
            "anime_enabled": False,
            "anime_window": "week",
            "anime_min_vote": 7.0,
            "anime_min_year": 0,
            "anime_count": 10,
        }

    def get_page(self) -> List[dict]:
        history = self.get_data('history') or []
        if not history:
            return [{'component': 'div', 'text': '暂无订阅历史', 'props': {'class': 'text-center mt-4'}}]
        
        history = sorted(history, key=lambda x: x.get('time'), reverse=True)[:50]
        contents = []
        for item in history:
            tmdb_link = f"https://www.themoviedb.org/{'movie' if item.get('type')=='电影' else 'tv'}/{item.get('tmdb_id')}"
            contents.append({
                'component': 'VCard',
                'props': {'class': 'mx-auto mb-2', 'width': '100%'},
                'content': [
                    {
                        'component': 'VCardItem',
                        'content': [
                            {'component': 'VCardTitle', 'text': item.get('title'), 'props': {'class': 'text-body-1 font-weight-bold'}},
                            {'component': 'VCardSubtitle', 'text': f"{item.get('type')} | {item.get('year')} | {item.get('source_type', '未知来源')}", 'props': {'class': 'text-caption'}},
                        ]
                    },
                    {
                        'component': 'VCardText',
                        'props': {'class': 'py-0'},
                        'content': [{'component': 'div', 'text': f"评分: {item.get('vote')} | 时间: {item.get('time')}", 'props': {'class': 'text-caption text-medium-emphasis'}}]
                    },
                    {
                        'component': 'VCardActions',
                        'content': [{'component': 'VBtn', 'props': {'href': tmdb_link, 'target': '_blank', 'variant': 'text', 'size': 'x-small', 'color': 'primary'}, 'text': '查看TMDB'}]
                    }
                ]
            })
        return [{'component': 'div', 'props': {'class': 'grid gap-3 grid-info-card'}, 'content': contents}]

    def stop_service(self):
        pass

    def sync_tmdb_trends(self):
        """核心业务逻辑"""
        logger.info("开始执行 TMDB 榜单订阅任务...")
        added_list = []
        
        # 1. 处理电影
        if self._movie_enabled:
            # 兼容处理：确保是列表
            sources = self._movie_sources if isinstance(self._movie_sources, list) else [self._movie_sources]
            genres = self._movie_genres if isinstance(self._movie_genres, list) else []

            for src in sources:
                if src == 'discover':
                    # 如果选了 Discover，遍历所有选中的分类
                    target_genres = genres if genres else [""] # 如果没选分类，传空串拉取默认发现
                    for genre_id in target_genres:
                        added_list.extend(self.__fetch_and_process(
                            media_type=MediaType.MOVIE,
                            source=src,
                            genre_id=genre_id,
                            min_vote=self._movie_min_vote,
                            min_year=self._movie_min_year,
                            limit=self._movie_count,
                            category_label="电影"
                        ))
                else:
                    # 其他榜单直接拉取
                    added_list.extend(self.__fetch_and_process(
                        media_type=MediaType.MOVIE,
                        source=src,
                        genre_id="",
                        min_vote=self._movie_min_vote,
                        min_year=self._movie_min_year,
                        limit=self._movie_count,
                        category_label="电影"
                    ))
        
        # 2. 处理电视剧
        if self._tv_enabled:
            sources = self._tv_sources if isinstance(self._tv_sources, list) else [self._tv_sources]
            genres = self._tv_genres if isinstance(self._tv_genres, list) else []

            for src in sources:
                if src == 'discover':
                    target_genres = genres if genres else [""]
                    for genre_id in target_genres:
                        added_list.extend(self.__fetch_and_process(
                            media_type=MediaType.TV,
                            source=src,
                            genre_id=genre_id,
                            min_vote=self._tv_min_vote,
                            min_year=self._tv_min_year,
                            limit=self._tv_count,
                            category_label="电视剧"
                        ))
                else:
                    added_list.extend(self.__fetch_and_process(
                        media_type=MediaType.TV,
                        source=src,
                        genre_id="",
                        min_vote=self._tv_min_vote,
                        min_year=self._tv_min_year,
                        limit=self._tv_count,
                        category_label="电视剧"
                    ))
            
        # 3. 处理动漫 (特殊逻辑)
        if self._anime_enabled:
            source = f"trending_{self._anime_window}"
            added_list.extend(self.__fetch_and_process(
                media_type=MediaType.TV,
                source=source,
                genre_id="", 
                min_vote=self._anime_min_vote,
                min_year=self._anime_min_year,
                limit=self._anime_count,
                category_label="动漫",
                is_anime_logic=True
            ))

        if self._notify and added_list:
            self.__send_notification(added_list)
        
        logger.info("TMDB 榜单订阅任务完成。")

    def __fetch_and_process(self, media_type: MediaType, source: str, genre_id: str, min_vote: float, min_year: int, limit: int, category_label: str, is_anime_logic: bool = False) -> List[dict]:
        """
        通用获取和处理逻辑
        """
        api_key = self._tmdb_api_key or settings.TMDB_API_KEY
        if not api_key:
            logger.error("未配置 TMDB API KEY")
            return []

        # 构建 URL
        base_url = "https://api.themoviedb.org/3"
        type_str = "tv" if media_type == MediaType.TV else "movie"
        url = ""
        params = f"api_key={api_key}&language=zh-CN"

        # 根据 Source 解析 URL
        if source == 'discover':
            url = f"{base_url}/discover/{type_str}?{params}&sort_by=popularity.desc"
            if genre_id:
                url += f"&with_genres={genre_id}"
        elif source.startswith('trending_'):
            window = source.split('_')[1] # day or week
            url = f"{base_url}/trending/{type_str}/{window}?{params}"
        elif source == 'now_playing':
            url = f"{base_url}/movie/now_playing?{params}"
        elif source == 'airing_today':
            url = f"{base_url}/tv/airing_today?{params}"
        elif source == 'on_the_air':
            url = f"{base_url}/tv/on_the_air?{params}"
        elif source == 'popular':
            url = f"{base_url}/{type_str}/popular?{params}"
        elif source == 'top_rated':
            url = f"{base_url}/{type_str}/top_rated?{params}"
        else:
            logger.error(f"未知的榜单来源: {source}")
            return []

        results = []
        page = 1
        
        # 循环翻页直到满足 limit (注意：对于Discovery+多分类的情况，这里的limit是针对【每个分类】的)
        while len(results) < limit and page <= 5:
            try:
                # 拼接 page 参数
                req_url = f"{url}&page={page}"
                response = RequestUtils().get_res(req_url)
                if not response: break
                
                data = response.json()
                items = data.get('results', [])
                if not items: break
                
                for item in items:
                    if len(results) >= limit: break
                    
                    # 评分过滤
                    if item.get('vote_average', 0) < min_vote: continue
                    
                    # 提取基础信息
                    tmdb_id = item.get('id')
                    title = item.get('title') if media_type == MediaType.MOVIE else item.get('name')
                    date = item.get('release_date') if media_type == MediaType.MOVIE else item.get('first_air_date')
                    year = date[:4] if date else ""

                    # 年份过滤
                    if min_year > 0:
                        if not year: 
                            continue 
                        try:
                            if int(year) < min_year:
                                continue
                        except ValueError:
                            continue

                    # 动漫特殊逻辑筛选
                    if is_anime_logic:
                        genre_ids = item.get('genre_ids', [])
                        origin_country = item.get('origin_country', [])
                        lang = item.get('original_language', '')
                        if not (16 in genre_ids and ('JP' in origin_country or lang == 'ja')):
                            continue
                    
                    # 去重
                    unique_key = f"{category_label}:{tmdb_id}"
                    if self.__is_processed(unique_key): continue
                    
                    # 添加订阅
                    if self.__add_subscribe(title, year, media_type, tmdb_id, category_label):
                        # 如果是 Discovery，记录具体的分类来源
                        display_source = source
                        if source == 'discover' and genre_id:
                            display_source = f"discover(genre:{genre_id})"

                        res_item = {
                            'title': title, 
                            'type': category_label, 
                            'vote': item.get('vote_average'), 
                            'tmdb_id': tmdb_id, 
                            'year': year,
                            'source_type': display_source
                        }
                        results.append(res_item)
                        self.__save_history(title, category_label, tmdb_id, item.get('vote_average'), unique_key, year, display_source)
                
                page += 1
            except Exception as e:
                logger.error(f"TMDB 请求失败: {e}")
                break
        
        return results

    def __add_subscribe(self, title, year, mtype, tmdb_id, category_name):
        try:
            meta = MetaInfo(title)
            meta.year = year
            mediainfo = MediaInfo()
            mediainfo.title = title
            mediainfo.year = year
            mediainfo.type = mtype
            mediainfo.tmdb_id = int(tmdb_id)
            
            if self.subscribechain.exists(mediainfo=mediainfo, meta=meta):
                return False
                
            self.subscribechain.add(title=title, year=year, mtype=mtype, tmdbid=int(tmdb_id), season=None, username="TMDB趋势插件")
            logger.info(f"[{category_name}] 订阅成功: {title}")
            return True
        except Exception as e:
            logger.error(f"订阅失败: {e}")
            return False

    def __is_processed(self, unique_key):
        history = self.get_data('history') or []
        return any(h.get('unique_key') == unique_key for h in history)

    def __save_history(self, title, category, tmdb_id, vote, unique_key, year, source):
        history = self.get_data('history') or []
        history.append({
            'title': title, 
            'type': category, 
            'tmdb_id': tmdb_id, 
            'vote': vote,
            'unique_key': unique_key, 
            'year': year, 
            'source_type': source,
            'time': datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })
        self.save_data('history', history[-500:])

    def __send_notification(self, items):
        if not items: return
        text = "\n".join([f"• [{i['type']}] {i['title']} ({i['year']} | {i['vote']}分)" for i in items])
        self.post_message(mtype=NotificationType.Subscribe, title=f"TMDB 订阅新增 {len(items)} 部", text=text)
