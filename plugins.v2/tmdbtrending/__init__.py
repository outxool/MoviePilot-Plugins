import datetime
import time
from threading import Event
from typing import Tuple, List, Dict, Any

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import MediaType, NotificationType
from app.utils.http import RequestUtils

class tmdbtrending(_PluginBase):
    # 插件基本信息
    plugin_name = "TMDB趋势订阅"
    plugin_desc = "自动订阅 TMDB 热门趋势（电影/电视剧/动漫），支持评分过滤与消息通知。"
    plugin_icon = "https://www.themoviedb.org/assets/2/v4/logos/v2/blue_square_2-d537fb228cf3ded904ef09b136fe3fec72548ebc1fea3fbbd1ad9e36364db38b.svg"
    plugin_version = "1.0.1"
    plugin_author = "MoviePilot-Plugins"
    plugin_config_prefix = "tmdbtrending_"
    plugin_order = 10
    auth_level = 1

    # 私有属性
    _scheduler = None
    _event = Event()
    subscribechain: SubscribeChain = None
    
    # 配置属性初始化
    _enabled = False
    _cron = "0 10 * * *"
    _onlyonce = False
    _notify = True
    _tmdb_api_key = ""
    
    # 电影配置
    _movie_enabled = False
    _movie_window = "day" # day or week
    _movie_min_vote = 7.0
    _movie_count = 10
    
    # 电视剧配置
    _tv_enabled = False
    _tv_window = "week"
    _tv_min_vote = 7.5
    _tv_count = 10
    
    # 动漫配置 (基于TV分类筛选)
    _anime_enabled = False
    _anime_window = "week"
    _anime_min_vote = 7.0
    _anime_count = 10

    def init_plugin(self, config: dict = None):
        self.subscribechain = SubscribeChain()
        
        # 读取配置
        if config:
            self._enabled = config.get("enabled", False)
            self._cron = config.get("cron", "0 10 * * *")
            self._onlyonce = config.get("onlyonce", False)
            self._notify = config.get("notify", True)
            self._tmdb_api_key = config.get("tmdb_api_key", "")
            
            # 电影
            self._movie_enabled = config.get("movie_enabled", False)
            self._movie_window = config.get("movie_window", "day")
            self._movie_min_vote = float(config.get("movie_min_vote", 7.0))
            self._movie_count = int(config.get("movie_count", 10))
            
            # 电视剧
            self._tv_enabled = config.get("tv_enabled", False)
            self._tv_window = config.get("tv_window", "week")
            self._tv_min_vote = float(config.get("tv_min_vote", 7.5))
            self._tv_count = int(config.get("tv_count", 10))
            
            # 动漫
            self._anime_enabled = config.get("anime_enabled", False)
            self._anime_window = config.get("anime_window", "week")
            self._anime_min_vote = float(config.get("anime_min_vote", 7.0))
            self._anime_count = int(config.get("anime_count", 10))

        # 停止现有服务
        self.stop_service()

        # 如果启用
        if self._enabled or self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            
            if self._cron and not self._onlyonce:
                try:
                    self._scheduler.add_job(
                        func=self.__run_task,
                        trigger=CronTrigger.from_crontab(self._cron),
                        name="TMDB趋势订阅"
                    )
                    logger.info(f"TMDB趋势订阅服务启动，周期：{self._cron}")
                except Exception as e:
                    logger.error(f"TMDB趋势订阅服务启动失败: {e}")
            
            if self._onlyonce:
                logger.info("TMDB趋势订阅：立即运行一次")
                self._scheduler.add_job(
                    func=self.__run_task,
                    trigger='date',
                    run_date=datetime.datetime.now(tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3)
                )
                self._onlyonce = False
                self.__update_config()

            if self._scheduler.get_jobs():
                self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        定义配置页面布局
        """
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
                                {'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '立即运行一次'}}
                            ]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [
                                {'component': 'VSwitch', 'props': {'model': 'notify', 'label': '发送通知'}}
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
                    # 电影配置区域
                    {'component': 'VAlert', 'props': {'type': 'info', 'text': '电影订阅配置 (Movies)', 'variant': 'tonal', 'class': 'mt-4'}},
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [
                                {'component': 'VSwitch', 'props': {'model': 'movie_enabled', 'label': '启用电影订阅'}}
                            ]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [
                                {'component': 'VSelect', 'props': {'model': 'movie_window', 'label': '趋势周期', 'items': [{'title': '今日趋势', 'value': 'day'}, {'title': '本周趋势', 'value': 'week'}]}}
                            ]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [
                                {'component': 'VTextField', 'props': {'model': 'movie_min_vote', 'label': '最低评分', 'type': 'number'}}
                            ]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [
                                {'component': 'VTextField', 'props': {'model': 'movie_count', 'label': '订阅数量', 'type': 'number'}}
                            ]}
                        ]
                    },
                    # 电视剧配置区域
                    {'component': 'VAlert', 'props': {'type': 'success', 'text': '电视剧订阅配置 (TV Shows)', 'variant': 'tonal', 'class': 'mt-4'}},
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [
                                {'component': 'VSwitch', 'props': {'model': 'tv_enabled', 'label': '启用剧集订阅'}}
                            ]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [
                                {'component': 'VSelect', 'props': {'model': 'tv_window', 'label': '趋势周期', 'items': [{'title': '今日趋势', 'value': 'day'}, {'title': '本周趋势', 'value': 'week'}]}}
                            ]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [
                                {'component': 'VTextField', 'props': {'model': 'tv_min_vote', 'label': '最低评分', 'type': 'number'}}
                            ]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [
                                {'component': 'VTextField', 'props': {'model': 'tv_count', 'label': '订阅数量', 'type': 'number'}}
                            ]}
                        ]
                    },
                    # 动漫配置区域
                    {'component': 'VAlert', 'props': {'type': 'warning', 'text': '动漫订阅配置 (Anime - 自动筛选日漫)', 'variant': 'tonal', 'class': 'mt-4'}},
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [
                                {'component': 'VSwitch', 'props': {'model': 'anime_enabled', 'label': '启用动漫订阅'}}
                            ]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [
                                {'component': 'VSelect', 'props': {'model': 'anime_window', 'label': '趋势周期', 'items': [{'title': '今日趋势', 'value': 'day'}, {'title': '本周趋势', 'value': 'week'}]}}
                            ]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [
                                {'component': 'VTextField', 'props': {'model': 'anime_min_vote', 'label': '最低评分', 'type': 'number'}}
                            ]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [
                                {'component': 'VTextField', 'props': {'model': 'anime_count', 'label': '订阅数量', 'type': 'number'}}
                            ]}
                        ]
                    },
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "notify": True,
            "cron": "0 10 * * *",
            "tmdb_api_key": "",
            "movie_enabled": False,
            "movie_window": "day",
            "movie_min_vote": 7.0,
            "movie_count": 10,
            "tv_enabled": False,
            "tv_window": "week",
            "tv_min_vote": 7.5,
            "tv_count": 10,
            "anime_enabled": False,
            "anime_window": "week",
            "anime_min_vote": 7.0,
            "anime_count": 10,
        }

    def get_page(self) -> List[dict]:
        """
        插件详情页 - 显示最近订阅历史 (优化后的UI布局)
        """
        history = self.get_data('history') or []
        if not history:
            return [
                {
                    'component': 'div',
                    'text': '暂无订阅历史，请在设置中勾选“立即运行一次”并保存。',
                    'props': {'class': 'text-center mt-4'}
                }
            ]
        
        # 按时间倒序
        history = sorted(history, key=lambda x: x.get('time'), reverse=True)[:50]
        
        contents = []
        for item in history:
            tmdb_link = f"https://www.themoviedb.org/{'movie' if item.get('type')=='电影' else 'tv'}/{item.get('tmdb_id')}"
            contents.append({
                'component': 'VCard',
                'props': {'class': 'mx-auto', 'width': '100%'},
                'content': [
                    {
                        'component': 'VCardItem',
                        'content': [
                            {'component': 'VCardTitle', 'text': item.get('title'), 'props': {'class': 'text-body-1 font-weight-bold'}},
                            {'component': 'VCardSubtitle', 'text': f"{item.get('type')} | {item.get('year')}", 'props': {'class': 'text-caption'}},
                        ]
                    },
                    {
                        'component': 'VCardText',
                        'props': {'class': 'py-0'},
                        'content': [
                            {'component': 'div', 'text': f"评分: {item.get('vote')} | 时间: {item.get('time')}", 'props': {'class': 'text-caption text-medium-emphasis'}}
                        ]
                    },
                    {
                        'component': 'VCardActions',
                        'content': [
                            {'component': 'VBtn', 'props': {'href': tmdb_link, 'target': '_blank', 'variant': 'text', 'size': 'x-small', 'color': 'primary'}, 'text': '查看TMDB'}
                        ]
                    }
                ]
            })
            
        return [
            {
                'component': 'div',
                'props': {'class': 'grid gap-3 grid-info-card'},
                'content': contents
            }
        ]

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            logger.error(f"TMDB插件停止失败: {e}")

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "notify": self._notify,
            "cron": self._cron,
            "tmdb_api_key": self._tmdb_api_key,
            "movie_enabled": self._movie_enabled,
            "movie_window": self._movie_window,
            "movie_min_vote": self._movie_min_vote,
            "movie_count": self._movie_count,
            "tv_enabled": self._tv_enabled,
            "tv_window": self._tv_window,
            "tv_min_vote": self._tv_min_vote,
            "tv_count": self._tv_count,
            "anime_enabled": self._anime_enabled,
            "anime_window": self._anime_window,
            "anime_min_vote": self._anime_min_vote,
            "anime_count": self._anime_count,
        })

    def __run_task(self):
        logger.info("开始执行 TMDB 趋势订阅任务...")
        added_list = []
        
        # 1. 处理电影
        if self._movie_enabled:
            added_list.extend(self.__process_tmdb_type(
                media_type=MediaType.MOVIE,
                window=self._movie_window,
                min_vote=self._movie_min_vote,
                limit=self._movie_count,
                category_name="电影"
            ))
            
        # 2. 处理电视剧
        if self._tv_enabled:
            added_list.extend(self.__process_tmdb_type(
                media_type=MediaType.TV,
                window=self._tv_window,
                min_vote=self._tv_min_vote,
                limit=self._tv_count,
                category_name="电视剧"
            ))
            
        # 3. 处理动漫 (特殊筛选)
        if self._anime_enabled:
            added_list.extend(self.__process_tmdb_type(
                media_type=MediaType.TV,
                window=self._anime_window,
                min_vote=self._anime_min_vote,
                limit=self._anime_count,
                category_name="动漫",
                is_anime=True
            ))

        # 发送通知
        if self._notify and added_list:
            self.__send_notification(added_list)
        
        logger.info("TMDB 趋势订阅任务完成。")

    def __process_tmdb_type(self, media_type: MediaType, window: str, min_vote: float, limit: int, category_name: str, is_anime: bool = False) -> List[dict]:
        """
        处理单个类型的订阅逻辑
        """
        api_key = self._tmdb_api_key or settings.TMDB_API_KEY
        if not api_key:
            logger.error("未配置 TMDB API KEY，无法获取数据")
            return []

        # 确定 URL
        # 动漫本质上是 TV，但我们需要筛选
        type_str = "tv" if media_type == MediaType.TV else "movie"
        url = f"https://api.themoviedb.org/3/trending/{type_str}/{window}?api_key={api_key}&language=zh-CN"
        
        results = []
        page = 1
        max_pages = 5 # 最多翻5页，防止无限循环
        
        while len(results) < limit and page <= max_pages:
            current_url = f"{url}&page={page}"
            try:
                # 使用 MP 的 RequestUtils，自动处理代理
                response = RequestUtils().get_res(current_url)
                if not response:
                    break
                data = response.json()
                items = data.get('results', [])
                if not items:
                    break
                
                for item in items:
                    if len(results) >= limit:
                        break
                        
                    # 评分过滤
                    vote_average = item.get('vote_average', 0)
                    if vote_average < min_vote:
                        continue
                        
                    # 动漫特殊过滤: Genre ID 16 (Animation) 且 原产国 JP 或 语言 ja
                    if is_anime:
                        genre_ids = item.get('genre_ids', [])
                        origin_country = item.get('origin_country', [])
                        original_language = item.get('original_language', '')
                        
                        is_animation = 16 in genre_ids
                        is_japanese = 'JP' in origin_country or original_language == 'ja'
                        
                        if not (is_animation and is_japanese):
                            continue
                    
                    # 避免动漫和普通剧集重复 (如果在普通剧集里排除了动漫则不需要，这里暂不做强制互斥，依靠去重逻辑)
                    
                    # 提取基本信息
                    tmdb_id = item.get('id')
                    title = item.get('title') if media_type == MediaType.MOVIE else item.get('name')
                    release_date = item.get('release_date') if media_type == MediaType.MOVIE else item.get('first_air_date')
                    year = release_date[:4] if release_date else ""
                    
                    # 去重检查 1: 历史记录
                    unique_key = f"{category_name}:{tmdb_id}"
                    if self.__is_processed(unique_key):
                        continue
                    
                    # 添加订阅
                    if self.__add_subscribe(title, year, media_type, tmdb_id, category_name):
                        # 记录成功
                        results.append({
                            'title': title,
                            'type': category_name,
                            'vote': vote_average,
                            'tmdb_id': tmdb_id,
                            'year': year
                        })
                        # 写入历史
                        self.__save_history(title, category_name, tmdb_id, vote_average, unique_key)
                
                page += 1
                time.sleep(0.5) # 礼貌请求
                
            except Exception as e:
                logger.error(f"获取 TMDB 数据失败 ({category_name}): {e}")
                break
                
        return results

    def __add_subscribe(self, title, year, mtype, tmdb_id, category_name):
        """
        调用 MP 核心订阅链添加订阅
        """
        try:
            # 检查是否已存在订阅
            # 构造 MetaInfo 用于检查
            meta = MetaInfo(title)
            meta.year = year
            
            # 构造 MediaInfo
            mediainfo = MediaInfo()
            mediainfo.title = title
            mediainfo.year = year
            mediainfo.type = mtype
            mediainfo.tmdb_id = int(tmdb_id)
            
            # 使用 SubscribeChain 检查 (数据库中是否存在)
            if self.subscribechain.exists(mediainfo=mediainfo, meta=meta):
                logger.info(f"[{category_name}] {title} ({year}) 已存在于订阅列表，跳过")
                return False
                
            # 添加订阅
            self.subscribechain.add(
                title=title,
                year=year,
                mtype=mtype,
                tmdbid=int(tmdb_id),
                season=None, # 电影为None, 电视剧全集订阅通常也传None或特定季，这里默认订阅所有
                username="TMDB趋势插件"
            )
            logger.info(f"[{category_name}] {title} ({year}) 添加订阅成功")
            return True
            
        except Exception as e:
            logger.error(f"添加订阅异常: {e}")
            return False

    def __is_processed(self, unique_key):
        """
        检查插件历史记录
        """
        history = self.get_data('history') or []
        for h in history:
            if h.get('unique_key') == unique_key:
                return True
        return False

    def __save_history(self, title, category, tmdb_id, vote, unique_key):
        """
        保存插件历史记录
        """
        history = self.get_data('history') or []
        history.append({
            'title': title,
            'type': category,
            'tmdb_id': tmdb_id,
            'vote': vote,
            'unique_key': unique_key,
            'year': self.__get_year_from_key(unique_key, title), # 简单补充year
            'time': datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })
        # 保持最近 500 条
        if len(history) > 500:
            history = history[-500:]
        self.save_data('history', history)
        
    def __get_year_from_key(self, key, title):
        # 辅助方法，仅用于兼容旧记录，新记录在process里已经存了
        return "" 

    def __send_notification(self, items):
        """
        发送通知
        """
        if not items:
            return
            
        title = f"TMDB 趋势订阅新增 {len(items)} 部影片"
        text = ""
        for item in items:
            text += f"• [{item['type']}] {item['title']} (评分: {item['vote']})\n"
            
        self.post_message(
            mtype=NotificationType.Subscribe, # 使用订阅类型的通知图标
            title=title,
            text=text
        )
