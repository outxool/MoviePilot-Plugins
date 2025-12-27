import datetime
import re
import xml.dom.minidom
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
from app.utils.dom import DomUtils
from app.utils.http import RequestUtils

# 兼容性导入
try:
    from app.schemas import MediaType, NotificationType
except ImportError:
    from app.schemas.types import MediaType, NotificationType

class DoubanRank(_PluginBase):
    # 插件基本信息
    plugin_name = "豆瓣榜单订阅增强版（自用）"
    plugin_desc = "监控豆瓣热门榜单，自动添加订阅。"
    plugin_icon = "movie.jpg"
    plugin_version = "2.0.4"
    plugin_author = "outxool"
    plugin_author_url = ""
    plugin_config_prefix = "doubanrank_"
    plugin_order = 6
    auth_level = 2

    _event = Event()
    _scheduler = None
    
    # 豆瓣RSS路径映射
    _douban_address = {
        'movie-ustop': '/douban/movie/ustop',
        'movie-weekly': '/douban/movie/weekly',
        'movie-real-time': '/douban/movie/weekly/movie_real_time_hotest',
        'show-domestic': '/douban/movie/weekly/show_domestic',
        'movie-hot-gaia': '/douban/movie/weekly/movie_hot_gaia',
        'tv-hot': '/douban/movie/weekly/tv_hot',
        'movie-top250': '/douban/movie/weekly/movie_top250',
        'movie-top250-full': '/douban/list/movie_top250',
    }
    
    # 配置项
    _enabled = False
    _cron = ""
    _onlyonce = False
    _rss_addrs = []
    _ranks = []
    _vote = 0
    _clear = False
    _proxy = False
    _rsshub = "https://rsshub.app"

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled", False)
            self._cron = config.get("cron", "")
            self._proxy = config.get("proxy", False)
            self._onlyonce = config.get("onlyonce", False)
            self._vote = float(config.get("vote") or 0)
            self._rsshub = config.get("rsshub") or "https://rsshub.app"
            
            rss_addrs = config.get("rss_addrs")
            if rss_addrs:
                if isinstance(rss_addrs, str):
                    self._rss_addrs = rss_addrs.split('\n')
                else:
                    self._rss_addrs = rss_addrs
            else:
                self._rss_addrs = []
                
            self._ranks = config.get("ranks") or []
            self._clear = config.get("clear", False)

        self.stop_service()

        if self._enabled or self._onlyonce:
            # 正常定时服务
            if self._enabled and self._cron:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._scheduler.add_job(func=self.__refresh_rss, trigger=CronTrigger.from_crontab(self._cron), name="豆瓣榜单订阅")
                if self._scheduler.get_jobs():
                    self._scheduler.start()
            elif self._enabled:
                # 默认每天8点
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._scheduler.add_job(func=self.__refresh_rss, trigger=CronTrigger.from_crontab("0 8 * * *"), name="豆瓣榜单订阅")
                if self._scheduler.get_jobs():
                    self._scheduler.start()

            # 执行一次性操作（立即运行/清理缓存）
            self.__execute_once_operations()

    def __execute_once_operations(self):
        """
        执行一次性操作并安全更新配置
        """
        config_updated = False
        
        # 1. 清理缓存
        if self._clear:
            self.save_data('history', [])
            self._clear = False
            config_updated = True
            logger.info("豆瓣榜单订阅：历史记录已清理")

        # 2. 立即运行
        if self._onlyonce:
            logger.info("豆瓣榜单订阅：检测到立即运行指令，正在后台执行...")
            Thread(target=self.__refresh_rss).start()
            self._onlyonce = False
            config_updated = True

        # 3. 回写配置（全量保存，防止丢失）
        if config_updated:
            self.__update_config()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/delete_history",
                "endpoint": self.delete_history,
                "methods": ["GET"],
                "summary": "删除豆瓣榜单订阅历史记录"
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            return [{
                "id": "DoubanRank",
                "name": "豆瓣榜单订阅服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.__refresh_rss,
                "kwargs": {}
            }]
        elif self._enabled:
            return [{
                "id": "DoubanRank",
                "name": "豆瓣榜单订阅服务",
                "trigger": CronTrigger.from_crontab("0 8 * * *"),
                "func": self.__refresh_rss,
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
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'proxy', 'label': '使用代理服务器'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '立即运行一次'}}]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VCronField', 'props': {'model': 'cron', 'label': '执行周期', 'placeholder': '5位cron表达式'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VTextField', 'props': {'model': 'vote', 'label': '评分', 'placeholder': '评分大于等于该值才订阅'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VTextField', 'props': {'model': 'rsshub', 'label': 'RSSHub地址', 'placeholder': 'https://rsshub.app'}}]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'content': [{'component': 'VSelect', 'props': {'chips': True, 'multiple': True, 'model': 'ranks', 'label': '热门榜单', 'items': [
                                {'title': '电影北美票房榜', 'value': 'movie-ustop'},
                                {'title': '一周口碑电影榜', 'value': 'movie-weekly'},
                                {'title': '实时热门电影', 'value': 'movie-real-time'},
                                {'title': '热门综艺', 'value': 'show-domestic'},
                                {'title': '热门电影', 'value': 'movie-hot-gaia'},
                                {'title': '热门电视剧', 'value': 'tv-hot'},
                                {'title': '电影TOP10', 'value': 'movie-top250'},
                                {'title': '电影TOP250', 'value': 'movie-top250-full'},
                            ]}}]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'content': [{'component': 'VTextarea', 'props': {'model': 'rss_addrs', 'label': '自定义榜单地址', 'placeholder': '每行一个地址'}}]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VSwitch', 'props': {'model': 'clear', 'label': '清理历史记录'}}]}
                        ]
                    }
                ]
            }
        ], {
            "enabled": False, "cron": "", "proxy": False, "onlyonce": False, "vote": "", 
            "rsshub": "https://rsshub.app", "ranks": [], "rss_addrs": "", "clear": False
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
                                {'component': 'VCardText', 'props': {'class': 'pa-0 px-2'}, 'text': f'类型：{history.get("type")}'},
                                {'component': 'VCardText', 'props': {'class': 'pa-0 px-2'}, 'text': f'时间：{history.get("time")}'}
                            ]}
                        ]
                    }
                ]
            })
        return [{'component': 'div', 'props': {'class': 'grid gap-3 grid-info-card'}, 'content': contents}]

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
            logger.error(str(e))

    def delete_history(self, key: str, apikey: str):
        if apikey != settings.API_TOKEN:
            return {"success": False, "message": "API密钥错误"}
        historys = self.get_data('history') or []
        historys = [h for h in historys if h.get("unique") != key]
        self.save_data('history', historys)
        return {"success": True, "message": "删除成功"}

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled, "cron": self._cron, "onlyonce": self._onlyonce,
            "vote": self._vote, "rsshub": self._rsshub, "ranks": self._ranks,
            "rss_addrs": '\n'.join(map(str, self._rss_addrs)), "clear": self._clear, "proxy": self._proxy
        })

    def __refresh_rss(self):
        logger.info(f"开始刷新豆瓣榜单 ...")
        rsshub_base = self._rsshub.rstrip('/')
        rank_addrs = [f"{rsshub_base}{self._douban_address.get(rank)}" for rank in self._ranks if self._douban_address.get(rank)]
        addr_list = self._rss_addrs + rank_addrs
        if not addr_list:
            logger.info(f"未设置榜单RSS地址")
            return

        history = self.get_data('history') or []

        for addr in addr_list:
            if not addr: continue
            try:
                rss_infos = self.__get_rss_info(addr)
                if not rss_infos: continue
                
                logger.info(f"RSS地址：{addr} ，共 {len(rss_infos)} 条数据")
                for rss_info in rss_infos:
                    if self._event.is_set(): return
                    
                    title = rss_info.get('title')
                    douban_id = rss_info.get('doubanid')
                    year = rss_info.get('year')
                    type_str = rss_info.get('type')
                    mtype = MediaType.MOVIE if type_str == "movie" else (MediaType.TV if type_str else None)
                    
                    unique_flag = f"doubanrank: {title} (DB:{douban_id})"
                    if unique_flag in [h.get("unique") for h in history]: continue
                    
                    # 元数据与媒体识别
                    meta = MetaInfo(title)
                    meta.year = year
                    if mtype: meta.type = mtype
                    
                    mediainfo = None
                    if douban_id:
                        if settings.RECOGNIZE_SOURCE == "themoviedb":
                            tmdbinfo = MediaChain().get_tmdbinfo_by_doubanid(doubanid=douban_id, mtype=meta.type)
                            if tmdbinfo:
                                meta.type = tmdbinfo.get('media_type')
                                mediainfo = self.chain.recognize_media(meta=meta, tmdbid=tmdbinfo.get("id"))
                        else:
                            mediainfo = self.chain.recognize_media(meta=meta, doubanid=douban_id)
                    else:
                        mediainfo = self.chain.recognize_media(meta=meta)
                        
                    if not mediainfo:
                        logger.warn(f'未识别到媒体信息: {title}')
                        continue
                        
                    # 评分过滤
                    if self._vote and mediainfo.vote_average < self._vote:
                        continue
                    
                    # 存在性检查 (媒体库 + 订阅)
                    exist_flag, _ = DownloadChain().get_no_exists_info(meta=meta, mediainfo=mediainfo)
                    if exist_flag: 
                        logger.info(f'{mediainfo.title_year} 媒体库中已存在')
                        continue
                    
                    subscribechain = SubscribeChain()
                    if subscribechain.exists(mediainfo=mediainfo, meta=meta): 
                        logger.info(f'{mediainfo.title_year} 订阅已存在')
                        continue
                    
                    # 添加订阅
                    subscribechain.add(title=mediainfo.title, year=mediainfo.year, mtype=mediainfo.type, 
                                       tmdbid=mediainfo.tmdb_id, season=meta.begin_season, exist_ok=True, username="豆瓣榜单")
                    
                    history.append({
                        "title": title, "type": mediainfo.type.value, "year": mediainfo.year,
                        "poster": mediainfo.get_poster_image(), "overview": mediainfo.overview,
                        "tmdbid": mediainfo.tmdb_id, "doubanid": douban_id,
                        "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "unique": unique_flag
                    })
            except Exception as e:
                logger.error(f"处理RSS {addr} 出错: {e}")

        self.save_data('history', history)
        logger.info(f"所有榜单RSS刷新完成")

    def __get_rss_info(self, addr) -> List[dict]:
        """
        获取RSS (修复版)
        """
        try:
            # 关键修复：添加 User-Agent 模拟浏览器
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            
            # 处理代理逻辑：仅当用户开启且系统配置了代理时生效
            req_proxy = None
            if self._proxy:
                if settings.PROXY:
                    req_proxy = settings.PROXY
                    logger.info(f"使用代理请求 RSS: {addr}")
                else:
                    logger.warning("插件开启了代理，但 MoviePilot 未配置系统代理(PROXY)，将尝试直连")
            
            # 构造请求工具
            if req_proxy:
                req = RequestUtils(proxies=req_proxy)
            else:
                req = RequestUtils()
                
            ret = req.get_res(addr, headers=headers)
            
            if not ret or ret.status_code != 200:
                logger.error(f"获取RSS失败: {addr}, 状态码: {ret.status_code if ret else 'None'}")
                return []
                
            dom_tree = xml.dom.minidom.parseString(ret.text)
            items = dom_tree.documentElement.getElementsByTagName("item")
            ret_array = []
            
            for item in items:
                try:
                    title = DomUtils.tag_value(item, "title", default="")
                    link = DomUtils.tag_value(item, "link", default="")
                    description = DomUtils.tag_value(item, "description", default="")
                    
                    if not title or not link: continue
                    
                    doubanid = re.findall(r"/(\d+)(?=/|$)", link)
                    doubanid = doubanid[0] if doubanid else None
                    
                    year = re.findall(r"\b(19\d{2}|20\d{2})\b", description)
                    year = year[0] if year else None
                    
                    ret_array.append({
                        'title': title,
                        'link': link,
                        'doubanid': doubanid,
                        'year': year,
                        'type': 'movie' 
                    })
                except Exception as e:
                    logger.debug(f"解析单条RSS失败: {e}")
            return ret_array
        except Exception as e:
            logger.error(f"解析RSS XML失败: {e}")
            return []
