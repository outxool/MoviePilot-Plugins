# TG 115自动转存 v0.6.0

v0.6.0 在 v0.5.0 全新直搜重构版基础上，新增资源质量检测、4K/2160p 优先、BDMV 蓝光结构默认跳过、自定义跳过结构/关键词、115 分享结构预检。

## 核心逻辑

插件会根据 MoviePilot 订阅名，直接搜索你配置的 TG 公开频道：

```text
https://t.me/s/频道?q=订阅名
```

搜到 115 分享后，插件即时完成：

```text
匹配订阅 → 判断缺失集/媒体状态 → 文本质量评分 → 高质量资源优先 → 检查重复处理记录 → 真实转存前预检115分享结构 → 按115限速规则转存
```

## 资源质量检测

默认开启资源质量检测：

- 优先 4K / 2160p / UHD。
- 默认最低分辨率：1080p。
- 默认跳过 CAM / TS / TC / 枪版 / 录屏等低质量关键词。
- 未知质量默认不自动转存。
- 同一订阅多个候选资源会按质量分排序，优先处理 4K/2160p。

## BDMV 与自定义跳过结构

默认只跳过明确的 BDMV 蓝光目录结构：

```text
BDMV/
BDMV 与 CERTIFICATE 同级出现
标题或目录名明确包含 BDMV
```

以下结构默认放行：

```text
VIDEO_TS
AUDIO_TS
ISO
原盘
DIY原盘
蓝光原盘
多个视频文件
多个顶层目录
sample / trailer / 花絮
合集 / 整季包 / 多版本包
```

如果需要额外跳过结构，在配置页“自定义跳过结构/关键词”中按行填写，例如：

```text
VIDEO_TS
.iso
原盘
sample
trailer
花絮
```

插件命中 BDMV 默认规则或自定义关键词时才会跳过。

## 演练模式与 115 保护

- 仅日志演练开启时，只做 TG 文本质量判断，不读取 115 分享结构，不真实转存。
- 关闭演练进行真实转存时，插件会只读读取 115 分享根目录用于结构预检。
- 每个订阅最多预检候选数默认 5，避免频繁读取 115。
- 遇到 `770004`、`已达到当前访问上限`、`访问频繁`、`稍后再试` 会进入现有限流冷却。

## state.db 保存什么

插件只保存最小处理记录：

- 哪些 115 分享已经处理过
- 哪些已经转存
- 哪些只是演练预览
- 哪些失败过几次
- 质量分、分辨率、质量标签、结构标签、跳过原因
- 追更计划
- 搜索运行摘要

这是为了防止重复转存和保护 115。

## 默认设置

- 仅日志演练：开启
- 新增订阅后自动搜索：开启
- 新增订阅等待：30 秒
- 定时搜索全部订阅：关闭
- 追更搜索：关闭
- 全网查询更新时间：关闭
- 更新时间后延迟搜索：35 分钟
- 资源质量检测：开启
- 优先 4K/2160p：开启
- 最低分辨率：1080p
- 跳过 BDMV 蓝光结构：开启
- 自定义跳过结构/关键词：空
- 每订阅最多预检候选：5
- 允许未知质量自动转存：关闭
- 质量分阈值：40

## 安装提醒

必须完整替换 `tg115autotransfer` 目录，不要只替换单个 `__init__.py`。

GitHub 仓库必须完整包含：

```text
plugins.v2/tg115autotransfer/__init__.py
plugins.v2/tg115autotransfer/models.py
plugins.v2/tg115autotransfer/telegram.py
plugins.v2/tg115autotransfer/searcher.py
plugins.v2/tg115autotransfer/matcher.py
plugins.v2/tg115autotransfer/records.py
plugins.v2/tg115autotransfer/transfer.py
plugins.v2/tg115autotransfer/quality.py
plugins.v2/tg115autotransfer/follow_schedule.py
plugins.v2/tg115autotransfer/text.py
plugins.v2/tg115autotransfer/p115.py
plugins.v2/tg115autotransfer/requirements.txt
plugins.v2/tg115autotransfer/README.md
```
