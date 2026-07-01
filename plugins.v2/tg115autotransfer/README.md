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


## v0.7.0 完整重构说明

升级前请先备份：`/config/plugins/tg115autotransfer/state.db`。

v0.7.0 重点修复和重构：

1. 演练记录 `previewed` 不再阻止后续真实转存；演练模式下仍会跳过已演练链接，关闭演练后同一链接可进入真实转存，成功后覆盖为 `transferred`。
2. “立即搜索全部订阅”和“定时搜索全部订阅”会真正遍历全部有效订阅，不再固定截取前 20 个；旧配置 `max_subscriptions_per_run` 仅兼容读取，不再限制全量搜索。
3. 一个完整任务只创建一个状态库对象、一个转存控制器和一个运行上下文；单轮转存上限、每订阅转存上限、转存间隔、质量预检次数、115 冷却状态在整轮任务中共享。
4. 明确季不一致会直接拒绝；无明确季信息的 `E05` 不再默认当第 1 季，而是按订阅季作为上下文处理。
5. TG 搜索按 `(channel, message_id)` 去重，同一消息被多个关键词命中只处理一次；同一订阅内跨频道重复发布的同一 115 分享在单轮内只处理一次。
6. 状态模型拆分为 `previewed`、`transferred`、`existing`、`need_confirm`、`failed_retryable`、`failed_final`、`skipped_permanent`、`deferred`、`skipped_duplicate`、`ignored`，区分永久跳过和临时延后。
7. 数据库迁移会幂等增加 `reason_code`、`retryable`、`retry_after`、`last_attempt_at`、追更每日计数字段和运行统计字段；旧 `skipped` 会按原因迁移为永久跳过或临时延后，无法识别时按临时延后处理。
8. 115 限流后写入冷却时间；若启用“限流后立刻停止整轮”，不会继续搜索后续资源和订阅。
9. 质量结构预检数量受 `max_quality_probe_per_subscription` 限制，达到上限时临时延后，下轮可重试。
10. `receive(selected_ids=...)` 不再重复调用 `list_share_root()`；目录列表和分享根目录支持分页读取；读取请求有有限重试，POST 不盲目重试。
11. 桥接事件改为整轮最多一次，使用 Timer 非阻塞延迟发送；插件关闭或保存配置会取消旧 Timer。
12. API 使用 `asyncio.to_thread()` 执行同步搜索，避免阻塞 MoviePilot 事件循环；并发点击时只有一个任务运行。
13. 通知开关接入自动任务；可按配置发送运行总结、空结果通知，失败/限流/转存优先通知。
14. 追更任务接入单剧每次最多转存、每部剧每日触发次数限制；锁忙、找不到订阅、限流会设置短期重试或冷却，不直接永久错过。
15. `p115client` 已声明为真实转存依赖；演练模式不需要 115 客户端，真实转存首次初始化时若缺依赖会给出明确错误。

回滚方式：停止 MoviePilot，恢复旧插件目录和备份的 `state.db` 后再启动。
