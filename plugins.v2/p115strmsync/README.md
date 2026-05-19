# p115strmsync

> 基于原版 `p115strmhelper` 修改，由 [`outxool/moviepilot-plugins`](https://github.com/outxool/moviepilot-plugins) 维护的独立插件

## 说明

- 本插件来源：`P115StrmHelper`
- 当前插件身份：`P115StrmSync`
- 当前目录名：`p115strmsync`
- 当前配置前缀：`p115strmsync_`
- 当前维护仓库：`https://github.com/outxool/moviepilot-plugins`
- 当前版本：`1.0.5`

## 修改备注

本插件不是原版发布源中的官方原名分支，而是基于原版 `P115StrmHelper` 复制并修改后的独立插件版本。

当前版本的主要目标：

1. 独立插件化，避免与原版 `P115StrmHelper` 身份冲突
2. 保留原版 `full sync` / `increment sync` 主链能力
3. 由 `outxool/moviepilot-plugins` 仓库维护，便于单独安装与验证
4. 交付包已清理 `__pycache__`、`*.pyc` 等运行缓存文件

## 对外标识

- 插件名称：`115网盘STRM同步`
- 插件类名：`P115StrmSync`
- manifest key：`P115StrmSync`
- 版本号：`1.0.5`
- 作者标识：`outxool（基于 DDSRem 原版修改）`
- 维护仓库：`https://github.com/outxool/moviepilot-plugins`
- package 图标：`https://raw.githubusercontent.com/outxool/moviepilot-plugins/main/icons/u115.png`

## 历史

- `v1.0.5`：收敛对外命令、API、服务与 dashboard，只保留 STRM 同步主链相关暴露面
- `v1.0.4`：重新整理独立发布包，仅保留 `p115strmsync` 所需目录与单插件 package 清单
- `v1.0.3`：清理打包缓存文件，`package.v2.json` 图标地址切换到 `outxool/moviepilot-plugins` 仓库
- `v1.0.2`：更新维护仓库与作者信息为 `outxool/moviepilot-plugins`，并保留原版来源备注
- `v1.0.1`：更新插件对外信息，明确标注基于原版 `P115StrmHelper` 修改
- `v1.0.0`：从 `P115StrmHelper` fork，独立插件化并保留 `full/increment sync` 主链
