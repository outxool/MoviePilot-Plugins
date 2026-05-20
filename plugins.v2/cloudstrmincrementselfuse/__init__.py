import os
import shutil
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytz
from fastapi import Query

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.chain.storage import StorageChain
from app.core.config import settings
from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import FileItem
from app.schemas.types import EventType
from app.utils.system import SystemUtils


class CloudStrmIncrementSelfUse(_PluginBase):
    # 插件名称
    plugin_name = "云盘Strm生成（自用增量版）"
    # 插件描述
    plugin_desc = "定时扫描云盘/CloudDrive2储存文件，生成Strm文件（自用增量版）。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/outxool/moviepilot-plugins/main/icons/create.png"
    # 插件版本
    plugin_version = "1.2.0"
    # 插件作者
    plugin_author = "outxool（基于 thsrite 原版自用修改）"
    # 作者主页
    author_url = "https://github.com/outxool/moviepilot-plugins"
    # 插件配置项ID前缀
    plugin_config_prefix = "cloudstrmincrementselfuse_"
    # 加载顺序
    plugin_order = 26
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _cron = None
    _monitor_confs = None
    _onlyonce = False
    _copy_files = False
    _https = False
    _no_del_dirs = None
    _rmt_mediaext = ".mp4, .mkv, .ts, .iso,.rmvb, .avi, .mov, .mpeg,.mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .strm,.tp, .f4v"
    _observer = []

    # 公开属性
    _increment_dir = {}
    _dirconf = {}
    _libraryconf = {}
    _cloudtypeconf = {}
    _cloudurlconf = {}
    _cloudpathconf = {}
    _storageconf = {}
    _structured_conf_slots = 5
    _structured_config = {}
    _last_generated_monitor_confs = ""

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 清空配置
        self._dirconf = {}
        self._libraryconf = {}
        self._cloudtypeconf = {}
        self._cloudurlconf = {}
        self._cloudpathconf = {}
        self._increment_dir = {}
        self._storageconf = {}
        self._structured_config = {}
        self._last_generated_monitor_confs = ""

        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._onlyonce = config.get("onlyonce")
            self._https = config.get("https")
            self._copy_files = config.get("copy_files")
            self._structured_config = self.__extract_structured_config(config)
            structured_monitor_confs = self.__build_monitor_confs_from_form(config)
            self._last_generated_monitor_confs = structured_monitor_confs
            # v1.2.0 起：可视化配置行直接生成并替代监控目录配置；旧 monitor_confs 仅作为兼容兜底
            self._monitor_confs = structured_monitor_confs or config.get("monitor_confs")
            self._no_del_dirs = config.get("no_del_dirs")
            self._rmt_mediaext = config.get(
                "rmt_mediaext") or ".mp4, .mkv, .ts, .iso,.rmvb, .avi, .mov, .mpeg,.mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .strm,.tp, .f4v"

        # 停止现有任务
        self.stop_service()

        if self._enabled or self._onlyonce:
            # 定时服务
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

            # 读取目录配置
            if not self._monitor_confs:
                logger.error("未配置目录监控，请在可视化配置行中至少启用一行")
                return
            monitor_confs = self._monitor_confs.split("\n")
            if not monitor_confs:
                return
            for monitor_conf in monitor_confs:
                # 格式 源目录:目的目录:媒体库内网盘路径:监控模式
                if not monitor_conf:
                    continue
                # 注释
                if str(monitor_conf).startswith("#"):
                    continue

                if str(monitor_conf).count("#") == 3:
                    increment_dir = str(monitor_conf).split("#")[0]
                    source_dir = str(monitor_conf).split("#")[1]
                    target_dir = str(monitor_conf).split("#")[2]
                    library_dir = str(monitor_conf).split("#")[3]
                    self._libraryconf[source_dir] = library_dir
                elif str(monitor_conf).count("#") == 5:
                    increment_dir = str(monitor_conf).split("#")[0]
                    source_dir = str(monitor_conf).split("#")[1]
                    target_dir = str(monitor_conf).split("#")[2]
                    cloud_type = str(monitor_conf).split("#")[3]
                    cloud_path = str(monitor_conf).split("#")[4]
                    cloud_url = str(monitor_conf).split("#")[5]
                    self._cloudtypeconf[source_dir] = cloud_type
                    self._cloudpathconf[source_dir] = cloud_path
                    self._cloudurlconf[source_dir] = cloud_url
                else:
                    logger.error(f"{monitor_conf} 格式错误")
                    continue

                # 存储目录监控配置
                self._dirconf[source_dir] = target_dir

                # CloudDrive2 存储插件目录配置，格式：cd2_storage://存储名/目录#...
                if str(increment_dir).startswith("cd2_storage://"):
                    storage_path = str(increment_dir).replace("cd2_storage://", "", 1).strip("/")
                    if not storage_path:
                        logger.error(f"{monitor_conf} CloudDrive2存储路径格式错误")
                        continue
                    storage_parts = storage_path.split("/", 1)
                    self._storageconf[increment_dir] = {
                        "storage": storage_parts[0],
                        "path": f"/{storage_parts[1]}" if len(storage_parts) > 1 and storage_parts[1] else "/"
                    }

                # 增量配置
                self._increment_dir[increment_dir] = source_dir

                # 检查媒体库目录是不是下载目录的子目录
                try:
                    if target_dir and Path(target_dir).is_relative_to(Path(source_dir)):
                        logger.warn(f"{target_dir} 是下载目录 {source_dir} 的子目录，无法监控")
                        self.systemmessage.put(f"{target_dir} 是下载目录 {source_dir} 的子目录，无法监控")
                        continue
                except Exception as e:
                    logger.debug(str(e))
                    pass

            # 运行一次定时服务
            if self._onlyonce:
                logger.info("云盘增量监控执行服务启动，立即运行一次")
                self._scheduler.add_job(func=self.scan, trigger='date',
                                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                        name="云盘增量监控")
                # 关闭一次性开关
                self._onlyonce = False
                # 保存配置
                self.__update_config()

            # 周期运行
            if self._cron:
                try:
                    self._scheduler.add_job(func=self.scan,
                                            trigger=CronTrigger.from_crontab(self._cron),
                                            name="云盘增量监控")
                except Exception as err:
                    logger.error(f"定时任务配置错误：{err}")
                    # 推送实时消息
                    self.systemmessage.put(f"执行周期配置错误：{err}")

            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    @eventmanager.register(EventType.PluginAction)
    def scan(self, event: Event = None):
        """
        扫描
        """
        if not self._enabled:
            logger.error("插件未开启")
            return
        if not self._dirconf or not self._dirconf.keys():
            logger.error("未获取到可用目录监控配置，请检查")
            return

        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "cloud_strm_increment_selfuse":
                return
            logger.info("收到命令，开始云盘strm生成 ...")
            self.post_message(channel=event.event_data.get("channel"),
                              title="开始云盘strm生成 ...",
                              userid=event.event_data.get("user"))

        logger.info("云盘strm生成任务开始")
        for increment_dir in self._increment_dir.keys():
            logger.info(f"正在扫描增量目录 {increment_dir}")
            if self.__is_cd2_storage_dir(increment_dir):
                self.__scan_cd2_storage(increment_dir)
                continue
            for root, dirs, files in os.walk(increment_dir):
                # 如果遇到名为'extrafanart'的文件夹，则跳过处理该文件夹，继续处理其他文件夹
                if "extrafanart" in dirs:
                    dirs.remove("extrafanart")

                # 处理文件
                for file in files:
                    increment_file = os.path.join(root, file)
                    # 回收站及隐藏的文件不处理
                    if (increment_file.find("/@Recycle") != -1
                            or increment_file.find("/#recycle") != -1
                            or increment_file.find("/.") != -1
                            or increment_file.find("/@eaDir") != -1):
                        logger.info(f"{increment_file} 是回收站或隐藏的文件，跳过处理")
                        continue

                    # 不复制非媒体文件时直接过滤掉非媒体文件
                    if not self._copy_files and Path(file).suffix not in [ext.strip() for ext in
                                                                          self._rmt_mediaext.split(",")]:
                        continue

                    logger.info(f"扫描到增量文件 {increment_file}，正在开始处理")

                    # 移动到目标目录
                    source_dir = self._increment_dir.get(increment_dir)
                    # 移动后文件
                    source_file = increment_file.replace(increment_dir, source_dir)

                    # 判断目标文件是否存在
                    if not Path(source_file).parent.exists():
                        Path(source_file).parent.mkdir(parents=True, exist_ok=True)

                    shutil.move(increment_file, source_file, copy_function=shutil.copy2)
                    logger.info(f"移动增量文件 {increment_file} 到 {source_file}")

                    # 扫描云盘文件，判断是否有对应strm
                    self.__strm(source_file)
                    logger.info(f"增量文件 {increment_file} 处理完成")

                    # 判断当前媒体父路径下是否有媒体文件，如有则无需遍历父级
                    if not SystemUtils.exits_files(Path(increment_file).parent,
                                                   [ext.strip() for ext in self._rmt_mediaext.split(",")]):
                        # 判断父目录是否为空, 为空则删除
                        for parent_path in Path(increment_file).parents:
                            if parent_path.name in self._no_del_dirs:
                                break
                            if str(parent_path.name) == str(increment_dir):
                                break
                            if str(parent_path.parent) != str(Path(increment_file).root):
                                # 父目录非根目录，才删除父目录
                                if not SystemUtils.exits_files(parent_path,
                                                               [ext.strip() for ext in self._rmt_mediaext.split(",")]):
                                    # 当前路径下没有媒体文件则删除
                                    shutil.rmtree(parent_path)
                                    logger.warn(f"增量非保留目录 {parent_path} 已删除")

        logger.info("云盘strm生成任务完成")
        if event:
            self.post_message(channel=event.event_data.get("channel"),
                              title="云盘strm生成任务完成！",
                              userid=event.event_data.get("user"))

    @staticmethod
    def __is_cd2_storage_dir(increment_dir: str) -> bool:
        """
        是否为 MoviePilot CloudDrive2 存储插件目录
        """
        return str(increment_dir).startswith("cd2_storage://")

    @staticmethod
    def __fileitem_attr(file_item, *names):
        """
        兼容不同 MoviePilot 版本 FileItem 字段名
        """
        for name in names:
            if hasattr(file_item, name):
                value = getattr(file_item, name)
                if value is not None:
                    return value
            if isinstance(file_item, dict) and file_item.get(name) is not None:
                return file_item.get(name)
        return None

    def __list_storage_files(self, storage_chain, storage_name: str, path: str):
        """
        通过 StorageChain 获取目录下文件，兼容不同 MoviePilot 版本的调用参数
        """
        file_item = FileItem(storage=storage_name, path=path)
        call_methods = [
            lambda: storage_chain.list_files(file_item),
            lambda: storage_chain.list_files(fileitem=file_item),
            lambda: storage_chain.list_files(file_item=file_item),
            lambda: storage_chain.list(file_item),
            lambda: storage_chain.list(fileitem=file_item),
            lambda: storage_chain.list(file_item=file_item),
            lambda: storage_chain.list(storage=storage_name, path=path),
            lambda: storage_chain.list(path=path, storage=storage_name),
            lambda: storage_chain.list(path),
        ]
        last_err = None
        for call_method in call_methods:
            try:
                files = call_method()
                return files or []
            except TypeError as err:
                last_err = err
                continue
        if last_err:
            raise last_err
        return []

    def __scan_cd2_storage(self, increment_dir: str):
        """
        扫描 MoviePilot CloudDrive2 存储插件目录，不依赖本地增量目录、不移动文件。
        """
        storage_conf = self._storageconf.get(increment_dir)
        if not storage_conf:
            logger.error(f"CloudDrive2存储配置不存在：{increment_dir}")
            return

        source_dir = self._increment_dir.get(increment_dir)
        storage_name = storage_conf.get("storage")
        storage_root = storage_conf.get("path") or "/"
        storage_chain = StorageChain()
        media_exts = [ext.strip() for ext in self._rmt_mediaext.split(",")]

        def scan_path(current_path: str):
            try:
                file_items = self.__list_storage_files(storage_chain, storage_name, current_path)
            except Exception as err:
                logger.error(f"扫描CloudDrive2存储目录失败 {storage_name}:{current_path} - {err}")
                return

            for file_item in file_items:
                item_path = self.__fileitem_attr(file_item, "path", "file_path", "filepath")
                item_name = self.__fileitem_attr(file_item, "name", "file_name", "filename")
                if not item_path and item_name:
                    item_path = f"{str(current_path).rstrip('/')}/{item_name}"
                if not item_path:
                    continue

                item_path = str(item_path).replace("\\", "/")
                item_name = str(item_name or Path(item_path).name)

                if (item_path.find("/@Recycle") != -1
                        or item_path.find("/#recycle") != -1
                        or item_path.find("/.") != -1
                        or item_path.find("/@eaDir") != -1):
                    logger.info(f"{item_path} 是回收站或隐藏的文件，跳过处理")
                    continue

                is_dir = self.__fileitem_attr(file_item, "is_dir", "is_directory", "directory")
                item_type = self.__fileitem_attr(file_item, "type", "file_type")
                if is_dir is None:
                    is_dir = str(item_type).lower() in ["dir", "directory", "folder"]

                if is_dir:
                    if item_name == "extrafanart":
                        continue
                    scan_path(item_path)
                    continue

                if not self._copy_files and Path(item_path).suffix not in media_exts:
                    continue

                rel_path = os.path.relpath(item_path, storage_root).replace("\\", "/")
                if rel_path.startswith(".."):
                    rel_path = item_path.lstrip("/")
                source_file = os.path.join(source_dir, rel_path)

                # 兼容原 cd2/alist URL 模式：STRM 内容使用 cloud_path + 相对路径，目标路径仍使用 source_dir 映射
                strm_source_file = None
                cloud_path = self._cloudpathconf.get(source_dir)
                if self._cloudtypeconf.get(source_dir) and cloud_path:
                    strm_source_file = os.path.join(cloud_path, rel_path)

                logger.info(f"扫描到CloudDrive2存储文件 {item_path}，映射为 {source_file}，正在开始处理")
                self.__strm(source_file=source_file, strm_source_file=strm_source_file, copy_source_file=False)
                logger.info(f"CloudDrive2存储文件 {item_path} 处理完成")

        scan_path(storage_root)

    # def move_file(self,
    #               file_path: Path,
    #               dest_path: Path,
    #               is_check_disk_space: bool = True,
    #               min_free_space: int = 300,
    #               wait_time: int = 300,
    #               check_paths: Optional[List[Path]] = None,
    #               ) -> bool:
    #     """
    #     移动文件,如果父文件夹为空,则删除空父文件夹
    #     """
    #     # 在目标路径存在时，会尝试覆盖它
    #     if not file_path.exists():
    #         logger.debug(f"move文件不存在,跳过处理: {file_path}")
    #
    #     if is_check_disk_space:
    #         if not check_paths:
    #             check_paths = [dest_path.parent]
    #         check_paths.append(data_path)
    #
    #         for check_path in check_paths:
    #             while check_disk_space(check_path, min_free_space):
    #                 logger.warning(
    #                     f"文件 {check_path} 空间不足,等待 {wait_time}s再处理:"
    #                     f" {file_path}"
    #                 )
    #                 sleep(wait_time)
    #
    #     logger.debug(f"移动文件: {file_path} -> {dest_path}")
    #
    #     # # 改用copy2,避免移动文件夹时,程序中断导致文件丢失
    #     # is_copyed = copy(file_path, dest_path)
    #     # # 复制成功才继续执行
    #     # if not is_copyed:
    #     #     logger.warning(f"移动文件失败: {file_path} -> {dest_path}")
    #     #     return False
    #
    #     # # 复制后再删除文件
    #     # logger.debug(f"已复制文件:{file_path}, 正在删除文件: {file_path}")
    #
    #     try:
    #         if not dest_path.parent.exists():
    #             dest_path.parent.mkdir(parents=True, exist_ok=True)
    #
    #         cloud_str = "/mnt/cloud"
    #         if str(file_path).startswith(cloud_str) and str(dest_path).startswith(
    #                 cloud_str
    #         ):
    #             # 如果是云盘路径，则使用重命名
    #             file_path.rename(dest_path)
    #         else:
    #             shutil.move(file_path, dest_path, copy_function=shutil.copy2)

    def __strm(self, source_file, strm_source_file: str = None, copy_source_file: bool = True):
        """
        判断文件是否有对应strm
        """
        try:
            # 获取文件的转移路径
            for source_dir in self._dirconf.keys():
                if str(source_file).startswith(source_dir):
                    # 转移路径
                    dest_dir = self._dirconf.get(source_dir)
                    # 媒体库容器内挂载路径
                    library_dir = self._libraryconf.get(source_dir)
                    # 云服务类型
                    cloud_type = self._cloudtypeconf.get(source_dir)
                    # 云服务挂载本地跟路径
                    cloud_path = self._cloudpathconf.get(source_dir)
                    # 云服务地址
                    cloud_url = self._cloudurlconf.get(source_dir)

                    # 转移后文件
                    dest_file = source_file.replace(source_dir, dest_dir)
                    # 如果是文件夹
                    if Path(dest_file).is_dir():
                        if not Path(dest_file).exists():
                            logger.info(f"创建目标文件夹 {dest_file}")
                            os.makedirs(dest_file)
                            continue
                    else:
                        # 非媒体文件
                        if Path(dest_file).exists():
                            logger.info(f"目标文件 {dest_file} 已存在")
                            continue

                        # 文件
                        if not Path(dest_file).parent.exists():
                            logger.info(f"创建目标文件夹 {Path(dest_file).parent}")
                            os.makedirs(Path(dest_file).parent)

                        # 视频文件创建.strm文件
                        if Path(dest_file).suffix in [ext.strip() for ext in self._rmt_mediaext.split(",")]:
                            # 创建.strm文件
                            self.__create_strm_file(scheme="https" if self._https else "http",
                                                    dest_file=dest_file,
                                                    dest_dir=dest_dir,
                                                    source_file=strm_source_file or source_file,
                                                    library_dir=library_dir,
                                                    cloud_type=cloud_type,
                                                    cloud_path=cloud_path,
                                                    cloud_url=cloud_url)
                        else:
                            if self._copy_files and copy_source_file:
                                # 其他nfo、jpg等复制文件
                                shutil.copy2(source_file, dest_file)
                                logger.info(f"复制其他文件 {source_file} 到 {dest_file}")
        except Exception as e:
            logger.error(f"create strm file error: {e}")
            print(str(e))

    @staticmethod
    def __create_strm_file(dest_file: str, dest_dir: str, source_file: str, library_dir: str = None,
                           cloud_type: str = None, cloud_path: str = None, cloud_url: str = None,
                           scheme: str = None):
        """
        生成strm文件
        :param library_dir:
        :param dest_dir:
        :param dest_file:
        """
        try:
            # 获取视频文件名和目录
            video_name = Path(dest_file).name
            # 获取视频目录
            dest_path = Path(dest_file).parent

            if not dest_path.exists():
                logger.info(f"创建目标文件夹 {dest_path}")
                os.makedirs(str(dest_path))

            # 构造.strm文件路径
            strm_path = os.path.join(dest_path, f"{os.path.splitext(video_name)[0]}.strm")
            # strm已存在跳过处理
            if Path(strm_path).exists():
                logger.info(f"strm文件已存在 {strm_path}")
                return

            logger.info(f"替换前本地路径:::{dest_file}")

            # 云盘模式
            if cloud_type:
                # 替换路径中的\为/
                dest_file = source_file.replace("\\", "/")
                dest_file = dest_file.replace(cloud_path, "")
                # 对盘符之后的所有内容进行url转码
                dest_file = urllib.parse.quote(dest_file, safe='')
                if str(cloud_type) == "cd2":
                    # 将路径的开头盘符"/mnt/user/downloads"替换为"http://localhost:19798/static/http/localhost:19798/False/"
                    dest_file = f"{scheme}://{cloud_url}/static/{scheme}/{cloud_url}/False/{dest_file}"
                    logger.info(f"替换后cd2路径:::{dest_file}")
                elif str(cloud_type) == "alist":
                    dest_file = f"{scheme}://{cloud_url}/d/{dest_file}"
                    logger.info(f"替换后alist路径:::{dest_file}")
                else:
                    logger.error(f"云盘类型 {cloud_type} 错误")
                    return
            else:
                # 本地挂载路径转为emby路径
                dest_file = dest_file.replace(dest_dir, library_dir)
                logger.info(f"替换后emby容器内路径:::{dest_file}")

            # 写入.strm文件
            with open(strm_path, 'w') as f:
                f.write(dest_file)

            logger.info(f"创建strm文件 {strm_path}")
        except Exception as e:
            logger.error(f"创建strm文件失败")
            print(str(e))

    def __update_config(self):
        """
        更新配置
        """
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "copy_files": self._copy_files,
            "https": self._https,
            "cron": self._cron,
            "monitor_confs": self._last_generated_monitor_confs or self._monitor_confs,
            "generated_monitor_confs": self._last_generated_monitor_confs or self._monitor_confs or "",
            "no_del_dirs": self._no_del_dirs,
            "rmt_mediaext": self._rmt_mediaext,
            **self._structured_config
        })

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        return [{
            "cmd": "/cloud_strm_increment_selfuse",
            "event": EventType.PluginAction,
            "desc": "云盘strm文件生成(自用增量版)",
            "category": "",
            "data": {
                "action": "cloud_strm_increment_selfuse"
            }
        }]

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        }]
        """
        if self._enabled and self._cron:
            return [{
                "id": "CloudStrmIncrementSelfUse",
                "name": "云盘strm文件生成服务（自用增量版）",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.scan,
                "kwargs": {}
            }]
        return []


    @classmethod
    def __slot_keys(cls, index: int) -> Dict[str, str]:
        return {
            "enabled": f"conf{index}_enabled",
            "scan_type": f"conf{index}_scan_type",
            "cd2_storage": f"conf{index}_cd2_storage",
            "cd2_path": f"conf{index}_cd2_path",
            "local_increment_dir": f"conf{index}_local_increment_dir",
            "source_dir": f"conf{index}_source_dir",
            "target_dir": f"conf{index}_target_dir",
            "strm_type": f"conf{index}_strm_type",
            "library_dir": f"conf{index}_library_dir",
            "cloud_path": f"conf{index}_cloud_path",
            "cloud_url": f"conf{index}_cloud_url",
        }

    @classmethod
    def __structured_defaults(cls) -> Dict[str, Any]:
        defaults = {}
        for index in range(1, cls._structured_conf_slots + 1):
            keys = cls.__slot_keys(index)
            defaults.update({
                keys["enabled"]: index == 1,
                keys["scan_type"]: "cd2_storage",
                keys["cd2_storage"]: "CloudDrive储存",
                keys["cd2_path"]: "/",
                keys["local_increment_dir"]: "",
                keys["source_dir"]: "",
                keys["target_dir"]: "",
                keys["strm_type"]: "library",
                keys["library_dir"]: "",
                keys["cloud_path"]: "",
                keys["cloud_url"]: "localhost:19798",
            })
        return defaults

    @classmethod
    def __extract_structured_config(cls, config: dict = None) -> Dict[str, Any]:
        defaults = cls.__structured_defaults()
        if not config:
            return defaults
        for key in defaults.keys():
            if key in config:
                defaults[key] = config.get(key)
        return defaults

    @staticmethod
    def __clean_path(value: Any) -> str:
        return str(value or "").strip()

    @classmethod
    def __build_monitor_confs_from_form(cls, config: dict = None) -> str:
        if not config:
            return ""
        lines = []
        for index in range(1, cls._structured_conf_slots + 1):
            keys = cls.__slot_keys(index)
            if not config.get(keys["enabled"]):
                continue
            scan_type = cls.__clean_path(config.get(keys["scan_type"])) or "cd2_storage"
            if scan_type == "cd2_storage":
                storage_name = cls.__clean_path(config.get(keys["cd2_storage"]))
                cd2_path = cls.__clean_path(config.get(keys["cd2_path"])) or "/"
                if not storage_name:
                    logger.error(f"可视化配置第{index}行未填写CloudDrive2储存名称，已跳过")
                    continue
                increment_dir = f"cd2_storage://{storage_name}/{cd2_path.strip('/')}" if cd2_path != "/" else f"cd2_storage://{storage_name}/"
            else:
                increment_dir = cls.__clean_path(config.get(keys["local_increment_dir"]))

            source_dir = cls.__clean_path(config.get(keys["source_dir"]))
            target_dir = cls.__clean_path(config.get(keys["target_dir"]))
            strm_type = cls.__clean_path(config.get(keys["strm_type"])) or "library"
            if not increment_dir or not source_dir or not target_dir:
                logger.error(f"可视化配置第{index}行缺少扫描目录/监控目录/目的目录，已跳过")
                continue

            if strm_type == "direct":
                library_dir = cls.__clean_path(config.get(keys["library_dir"]))
                if not library_dir:
                    logger.error(f"可视化配置第{index}行选择直写路径但未填写媒体服务器内源文件路径，已跳过")
                    continue
                lines.append(f"{increment_dir}#{source_dir}#{target_dir}#{library_dir}")
            else:
                cloud_type = "alist" if strm_type == "alist" else "cd2"
                cloud_path = cls.__clean_path(config.get(keys["cloud_path"])) or source_dir
                cloud_url = cls.__clean_path(config.get(keys["cloud_url"])) or "localhost:19798"
                lines.append(f"{increment_dir}#{source_dir}#{target_dir}#{cloud_type}#{cloud_path}#{cloud_url}")
        return "\n".join(lines)

    def __get_config_value(self, key: str, default: Any = None) -> Any:
        return self._structured_config.get(key, default)

    def __structured_form_rows(self) -> List[Dict[str, Any]]:
        rows = []
        for index in range(1, self._structured_conf_slots + 1):
            rows.append(self.__structured_form_row(index))
        return rows

    def __structured_form_row(self, index: int) -> Dict[str, Any]:
        keys = self.__slot_keys(index)
        return {
            'component': 'VCard',
            'props': {
                'variant': 'outlined',
                'class': 'mb-3'
            },
            'content': [
                {
                    'component': 'VCardTitle',
                    'text': f'配置行 {index}'
                },
                {
                    'component': 'VCardText',
                    'content': [
                        {
                            'component': 'VRow',
                            'content': [
                                {'component': 'VCol', 'props': {'cols': 12, 'md': 2}, 'content': [{'component': 'VSwitch', 'props': {'model': keys['enabled'], 'label': '启用本行'}}]},
                                {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSelect', 'props': {'model': keys['scan_type'], 'label': '扫描来源', 'items': [{'title': 'CloudDrive2储存插件', 'value': 'cd2_storage'}, {'title': '本地增量目录', 'value': 'local'}]}}]},
                                {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VTextField', 'props': {'model': keys['cd2_storage'], 'label': 'CloudDrive2储存名称', 'placeholder': 'CloudDrive储存', 'hint': '扫描来源=CloudDrive2储存插件时必填，填写MP里CloudDrive2储存插件显示的储存名称', 'persistent_hint': True}}]},
                                {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VTextField', 'props': {'model': keys['cd2_path'], 'label': 'CloudDrive2云盘目录', 'placeholder': '/甲骨文98py/未整理', 'hint': '扫描来源=CloudDrive2储存插件时必填；这是云盘内路径，不是容器本地路径，请直接输入/粘贴', 'persistent_hint': True}}]},
                            ]
                        },
                        {
                            'component': 'VRow',
                            'content': [
                                {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VCombobox', 'props': {'model': keys['local_increment_dir'], 'label': '本地增量目录', 'items': self.__directory_options('/'), 'clearable': True, 'placeholder': '/downloads/increment', 'hint': '扫描来源选择“本地增量目录”时填写', 'persistent_hint': True}}]},
                                {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VCombobox', 'props': {'model': keys['source_dir'], 'label': '监控目录 / 源文件映射目录', 'items': self.__directory_options('/'), 'clearable': True, 'placeholder': '/115open/甲骨文98py/未整理', 'hint': '这里就是原“监控目录”的第二段；STRM内容会按此路径或URL映射', 'persistent_hint': True}}]},
                                {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VCombobox', 'props': {'model': keys['target_dir'], 'label': '目的目录 / STRM输出目录', 'items': self.__directory_options('/'), 'clearable': True, 'placeholder': '/媒体库/STRM-AV/未整理'}}]},
                            ]
                        },
                        {
                            'component': 'VRow',
                            'content': [
                                {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSelect', 'props': {'model': keys['strm_type'], 'label': 'STRM写入方式', 'items': [{'title': '直写媒体服务器内源文件路径', 'value': 'direct'}, {'title': '生成 CloudDrive2 URL', 'value': 'cd2'}, {'title': '生成 Alist URL', 'value': 'alist'}]}}]},
                                {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VCombobox', 'props': {'model': keys['library_dir'], 'label': '媒体服务器内源文件路径', 'items': self.__directory_options('/'), 'clearable': True, 'placeholder': '/115open/甲骨文98py/未整理', 'hint': 'STRM写入方式=直写时使用', 'persistent_hint': True}}]},
                                {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VCombobox', 'props': {'model': keys['cloud_path'], 'label': 'CD2/Alist挂载本地根路径', 'items': self.__directory_options('/'), 'clearable': True, 'placeholder': '/115open/甲骨文98py/未整理', 'hint': 'STRM写入方式=URL时使用；不填默认等于监控目录', 'persistent_hint': True}}]},
                                {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VTextField', 'props': {'model': keys['cloud_url'], 'label': 'CD2/Alist服务地址', 'placeholder': 'localhost:19798'}}]},
                            ]
                        }
                    ]
                }
            ]
        }

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/browse_dir",
                "endpoint": self.browse_dir_api,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "浏览本地目录",
            }
        ]

    def browse_dir_api(self, path: str = Query(default="/", description="目录路径")) -> Dict[str, Any]:
        """
        浏览 MoviePilot 容器内本地目录，用于后续前端目录选择
        """
        return self.__browse_dir(path)

    @staticmethod
    def __browse_dir(path: str = "/") -> Dict[str, Any]:
        try:
            current_path = Path(path or "/")
            if not current_path.exists():
                return {"code": 1, "msg": f"目录不存在: {current_path}", "data": {"path": str(current_path), "items": []}}
            if not current_path.is_dir():
                current_path = current_path.parent

            items = []
            if str(current_path) != str(current_path.parent):
                items.append({
                    "title": ".. 上级目录",
                    "value": str(current_path.parent),
                    "name": "..",
                    "path": str(current_path.parent),
                    "is_dir": True
                })

            for item in current_path.iterdir():
                if item.is_dir():
                    item_path = str(item)
                    items.append({
                        "title": item_path,
                        "value": item_path,
                        "name": item.name,
                        "path": item_path,
                        "is_dir": True
                    })

            items = sorted(items, key=lambda x: (x["name"] != "..", x["name"].lower()))
            return {"code": 0, "msg": "success", "data": {"path": str(current_path), "items": items}}
        except Exception as err:
            logger.error(f"浏览本地目录失败: {err}")
            return {"code": 1, "msg": f"浏览本地目录失败: {err}", "data": {"path": path, "items": []}}

    @staticmethod
    def __directory_options(root_path: str = "/") -> List[Dict[str, str]]:
        """
        获取配置页下拉目录选项
        """
        try:
            root = Path(root_path or "/")
            if not root.exists() or not root.is_dir():
                root = Path("/")
            options = [{"title": str(root), "value": str(root)}]
            for item in root.iterdir():
                if item.is_dir():
                    item_path = str(item)
                    options.append({"title": item_path, "value": item_path})
            return sorted(options, key=lambda x: x["title"].lower())
        except Exception as err:
            logger.error(f"获取目录下拉选项失败: {err}")
            return [{"title": "/", "value": "/"}]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'copy_files',
                                            'label': '复制非媒体文件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'https',
                                            'label': '启用https',
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '生成周期',
                                            'placeholder': '0 0 * * *'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'no_del_dirs',
                                            'label': '保留路径',
                                            'placeholder': 'series、movies、downloads、others'
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VAlert',
                        'props': {
                            'type': 'warning',
                            'variant': 'tonal',
                            'class': 'mb-3',
                            'text': 'v1.2.0 已改为可视化配置行：下面每一行会直接生成并替代原 monitor_confs 监控目录配置。保存后插件运行只读取已启用的可视化配置行；旧版多行文本只保留兼容迁移，不再作为主要入口。'
                        }
                    },
                    *self.__structured_form_rows(),
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'generated_monitor_confs',
                                            'label': '自动生成的监控目录配置（只读核对）',
                                            'rows': 4,
                                            'readonly': True,
                                            'placeholder': '保存后这里会显示由上方可视化配置行自动生成的实际 monitor_confs；插件运行读取的就是这些配置行。'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'rmt_mediaext',
                                            'label': '视频格式',
                                            'rows': 2,
                                            'placeholder': ".mp4, .mkv, .ts, .iso,.rmvb, .avi, .mov, .mpeg,.mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .strm,.tp, .f4v"
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '目录监控格式：'
                                                    '1.增量目录#监控目录#目的目录#媒体服务器内源文件路径；'
                                                    '2.增量目录#监控目录#目的目录#cd2#cd2挂载本地跟路径#cd2服务地址；'
                                                    '3.增量目录#监控目录#目的目录#alist#alist挂载本地跟路径#alist服务地址；'
                                                    '4.CloudDrive2储存插件扫描：cd2_storage://储存名称/云盘目录#监控目录#目的目录#媒体服务器内源文件路径，或继续使用#cd2#cd2挂载本地根路径#cd2服务地址。'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '媒体服务器内源文件路径：源文件目录即云盘挂载到媒体服务器的路径。'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'success',
                                            'variant': 'tonal'
                                        },
                                        'content': [
                                            {
                                                'component': 'span',
                                                'text': '配置教程请参考：'
                                            },
                                            {
                                                'component': 'a',
                                                'props': {
                                                    'href': 'https://raw.githubusercontent.com/outxool/moviepilot-plugins/main/docs/cloudstrmincrementselfuse/README.md',
                                                    'target': '_blank'
                                                },
                                                'text': 'https://raw.githubusercontent.com/outxool/moviepilot-plugins/main/docs/cloudstrmincrementselfuse/README.md'
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    },
                ]
            }
        ], {
            "enabled": False,
            "cron": "",
            "onlyonce": False,
            "copy_files": False,
            "https": False,
            "monitor_confs": "",
            "generated_monitor_confs": "",
            **self.__structured_defaults(),
            "no_del_dirs": "",
            "rmt_mediaext": ".mp4, .mkv, .ts, .iso,.rmvb, .avi, .mov, .mpeg,.mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .strm,.tp, .f4v"
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))
