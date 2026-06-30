# TG 115自动转存 v0.5.0

v0.5.0 是全新直搜重构版。

## 核心逻辑

插件会根据 MoviePilot 订阅名，直接搜索你配置的 TG 公开频道：

```text
https://t.me/s/频道?q=订阅名
```

搜到 115 分享后，插件即时完成：

```text
匹配订阅 → 判断缺失集/媒体状态 → 检查重复处理记录 → 按115限速规则转存
```

## state.db 保存什么

插件只保存最小处理记录：

- 哪些 115 分享已经处理过
- 哪些已经转存
- 哪些只是演练预览
- 哪些失败过几次
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
plugins.v2/tg115autotransfer/follow_schedule.py
plugins.v2/tg115autotransfer/text.py
plugins.v2/tg115autotransfer/p115.py
plugins.v2/tg115autotransfer/requirements.txt
plugins.v2/tg115autotransfer/README.md
```
