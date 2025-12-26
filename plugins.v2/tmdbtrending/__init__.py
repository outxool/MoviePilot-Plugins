# 强制打印日志，证明文件被系统加载
print("加载 TmdbTrending 插件模块...")

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
    plugin_desc = "自动订阅 TMDB 热门趋势（电影/电视剧/动漫），支持评分过滤与消息通知。"
    plugin_icon = "https://www.themoviedb.org/assets/2/v4/logos/v2/blue_square_2-d537fb228cf3ded904ef09b136fe3fec72548ebc1fea3fbbd1ad9e36364db38b.svg"
    plugin_version = "1.0.7"
    plugin_author = "MoviePilot-Plugins"
    plugin_config_prefix = "tmdbtrending_"
    plugin_order = 10
    auth_level = 1

    # 私有属性
    subscribechain: SubscribeChain = None
    
    # 配置属性
    _enabled = False
    _cron = "0 10 * * *"
    _notify = True
    _onlyonce = False
    _clear_history = False
    _tmdb_api_key = ""
    
    # 电影配置
    _movie_enabled = False
    _movie_window = "day"
    _movie_min_vote = 7.0
    _movie_count = 10
    
    # 电视剧配置
    _tv_enabled = False
    _tv_window = "week"
    _tv_min_vote = 7.5
    _tv_count = 10
    
    # 动漫配置
    _anime_enabled = False
    _anime_window = "week"
    _anime_min_vote = 7.0
    _anime_count = 10

    def init_plugin(self, config: dict = None):
        """
        插件初始化
        """
        logger.info("正在初始化 TMDB 趋势订阅插件...")
        self.subscribechain = SubscribeChain()
        
        if config:
            self._enabled = config.get("enabled", False)
            self._cron = config.get("cron", "0 10 * * *")
            self._notify = config.get("notify", True)
            self._onlyonce = config.get("onlyonce", False)
            self._clear_history = config.get("clear_history", False)
            self._tmdb_api_key = config.get("tmdb_api_key", "")
            
            self._movie_enabled = config.get("movie_enabled", False)
            self._movie_window = config.get("movie_window", "day")
            self._movie_min_vote = float(config.get("movie_min_vote", 7.0))
            self._movie_count = int(config.get("movie_count", 10))
            
            self._tv_enabled = config.get("tv_enabled", False)
            self._tv_window = config.get("tv_window", "week")
            self._tv_min_vote = float(config.get("tv_min_vote", 7.5))
            self._tv_count = int(config.get("tv_count", 10))
            
            self._anime_enabled = config.get("anime_enabled", False)
            self._anime_window = config.get("anime_window", "week")
            self._anime_min_vote = float(config.get("anime_min_vote", 7.0))
            self._anime_count = int(config.get("anime_count", 10))

        # 执行一次性操作
        self.__execute_once_operations()

    def __execute_once_operations(self):
        """
        执行清理历史或立即运行的操作，并重置开关
        """
        config_updated = False
        update_dict = {}

        # 1. 清除历史记录
        if self._clear_history:
            logger.info("TMDB趋势订阅：正在清除历史记录...")
            self.save_data('history', [])
            self._clear_history = False
            update_dict["clear_history"] = False
            config_updated = True
            logger.info("TMDB趋势订阅：历史记录已清除。")

        # 2. 立即运行一次 (使用线程防止阻塞保存)
        if self._onlyonce:
            logger.info("TMDB趋势订阅：检测到“立即运行”指令，正在后台启动任务...")
            Thread(target=self.sync_tmdb_trends).start()
            self._onlyonce = False
            update_dict["onlyonce"] = False
            config_updated = True
        
        # 3. 如果有变更，回写配置
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
        """
        注册定时服务
        """
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
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'movie_enabled', 'label': '启用'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSelect', 'props': {'model': 'movie_window', 'label': '周期', 'items': [{'title': '今日', 'value': 'day'}, {'title': '本周', 'value': 'week'}]}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VTextField', 'props': {'model': 'movie_min_vote', 'label': '最低分', 'type': 'number'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VTextField', 'props': {'model': 'movie_count', 'label': '数量', 'type': 'number'}}]}
                        ]
                    },
                    # 电视剧配置
                    {'component': 'VAlert', 'props': {'type': 'success', 'text': '电视剧订阅配置', 'variant': 'tonal', 'class': 'mt-4'}},
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'tv_enabled', 'label': '启用'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSelect', 'props': {'model': 'tv_window', 'label': '周期', 'items': [{'title': '今日', 'value': 'day'}, {'title': '本周', 'value': 'week'}]}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VTextField', 'props': {'model': 'tv_min_vote', 'label': '最低分', 'type': 'number'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VTextField', 'props': {'model': 'tv_count', 'label': '数量', 'type': 'number'}}]}
                        ]
                    },
                    # 动漫配置
                    {'component': 'VAlert', 'props': {'type': 'warning', 'text': '动漫订阅配置 (自动筛选日漫)', 'variant': 'tonal', 'class': 'mt-4'}},
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'anime_enabled', 'label': '启用'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSelect', 'props': {'model': 'anime_window', 'label': '周期', 'items': [{'title': '今日', 'value': 'day'}, {'title': '本周', 'value': 'week'}]}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VTextField', 'props': {'model': 'anime_min_vote', 'label': '最低分', 'type': 'number'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VTextField', 'props': {'model': 'anime_count', 'label': '数量', 'type': 'number'}}]}
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
                            {'component': 'VCardSubtitle', 'text': f"{item.get('type')} | {item.get('year')}", 'props': {'class': 'text-caption'}},
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
        """退出插件"""
        pass

    def sync_tmdb_trends(self):
        """核心业务逻辑，由系统调度调用"""
        logger.info("开始执行 TMDB 趋势订阅任务...")
        added_list = []
        
        if self._movie_enabled:
            added_list.extend(self.__process_tmdb_type(MediaType.MOVIE, self._movie_window, self._movie_min_vote, self._movie_count, "电影"))
        
        if self._tv_enabled:
            added_list.extend(self.__process_tmdb_type(MediaType.TV, self._tv_window, self._tv_min_vote, self._tv_count, "电视剧"))
            
        if self._anime_enabled:
            added_list.extend(self.__process_tmdb_type(MediaType.TV, self._anime_window, self._anime_min_vote, self._anime_count, "动漫", is_anime=True))

        if self._notify and added_list:
            self.__send_notification(added_list)
        
        logger.info("TMDB 趋势订阅任务完成。")

    def __process_tmdb_type(self, media_type: MediaType, window: str, min_vote: float, limit: int, category_name: str, is_anime: bool = False) -> List[dict]:
        api_key = self._tmdb_api_key or settings.TMDB_API_KEY
        if not api_key:
            logger.error("未配置 TMDB API KEY")
            return []

        type_str = "tv" if media_type == MediaType.TV else "movie"
        url = f"https://api.themoviedb.org/3/trending/{type_str}/{window}?api_key={api_key}&language=zh-CN"
        
        results = []
        page = 1
        
        while len(results) < limit and page <= 5:
            try:
                response = RequestUtils().get_res(f"{url}&page={page}")
                if not response: break
                items = response.json().get('results', [])
                if not items: break
                
                for item in items:
                    if len(results) >= limit: break
                    if item.get('vote_average', 0) < min_vote: continue
                    
                    if is_anime:
                        if not (16 in item.get('genre_ids', []) and ('JP' in item.get('origin_country', []) or item.get('original_language', '') == 'ja')):
                            continue
                    
                    tmdb_id = item.get('id')
                    title = item.get('title') if media_type == MediaType.MOVIE else item.get('name')
                    date = item.get('release_date') if media_type == MediaType.MOVIE else item.get('first_air_date')
                    year = date[:4] if date else ""
                    unique_key = f"{category_name}:{tmdb_id}"
                    
                    if self.__is_processed(unique_key): continue
                    
                    if self.__add_subscribe(title, year, media_type, tmdb_id, category_name):
                        res_item = {'title': title, 'type': category_name, 'vote': item.get('vote_average'), 'tmdb_id': tmdb_id, 'year': year}
                        results.append(res_item)
                        self.__save_history(title, category_name, tmdb_id, item.get('vote_average'), unique_key, year)
                
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

    def __save_history(self, title, category, tmdb_id, vote, unique_key, year):
        history = self.get_data('history') or []
        history.append({
            'title': title, 'type': category, 'tmdb_id': tmdb_id, 'vote': vote,
            'unique_key': unique_key, 'year': year, 'time': datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })
        self.save_data('history', history[-500:])

    def __send_notification(self, items):
        if not items: return
        text = "\n".join([f"• [{i['type']}] {i['title']} ({i['vote']}分)" for i in items])
        self.post_message(mtype=NotificationType.Subscribe, title=f"TMDB 趋势订阅新增 {len(items)} 部", text=text)
