# Steadflow Console

`console/` 是 `Steadflow` 的主后台项目，对应原 `sub2api` 主体。它承载服务端 API、用户控制台和管理后台；`china-models` 仅作为域名和技术兼容名称。本地/线上部署配置统一放在仓库根目录 `deploy/`。

## 项目职责

- 用户控制台与管理后台
- 账户认证、订阅、API Key、配额与统计接口
- 订阅代充及 Codex 中转相关权益履约
- 付费教程、Skills、workflow、Agents 的产品权益与分发配置
- 客户端使用的公开设置接口
- Skills Pack 管理后台配置与静态分发目录
- Docker 镜像运行入口

## 关键目录

- `backend/`
  - Go 后端主体
  - 包含 `cmd/`、`internal/`、`migrations/`、`resources/`
- `frontend/`
  - 控制台与管理后台前端
  - `public/logo.png` 是当前全工作区统一 logo 源
- `deploy/`
  - 仅保留 console 镜像构建需要的 `docker-entrypoint.sh`
  - 本地与生产部署相关脚本、compose、Caddy 配置统一看仓库根目录 `../deploy/`
- `data/`
  - 本地运行期数据目录
  - 仅用于本地 Docker / 本地后端联调
  - 默认不作为正式发布资产来源
- `docs/`
  - 后台、产品与内部说明文档
- `tools/`
  - 辅助工具

## 与其他项目的联动

- `app/`
  - 客户端通过本项目的公开 API、公开设置、skills manifest 和下载链接工作
- `skills/`
  - Skills Pack 源码与打包脚本已迁出；本项目只负责配置、分发和后台管理
- `site/`
  - 官网/帮助内容独立维护，不再放回本仓库
- `marketing/`
  - 营销内容独立维护，不在本仓库承载

## 当前拆分后的边界

业务边界：ChatGPT 账号不属于业务范围；科学上网只作为教程指导，不提供节点、线路、账号或售卖能力。商业化方向为订阅代充、中转服务、Codex 付费教程和付费 Skills/workflow/Agents。

以下内容已经迁出本仓库：

- 官网与帮助中心 -> `../site`
- Codex 桌面客户端 -> `../app`
- 营销资料 -> `../marketing`
- 推荐 Skills Pack 源码与打包脚本 -> `../skills`

本仓库保留的是“后台与分发中心”职责，而不是继续作为所有内容的大杂烩。

## `data/` 与部署目录的边界

- `data/`
  - 本地运行期目录
  - 给本地 Docker、`go run`、本地客户端下载联调用
  - 里面的文件不应默认视为仓库正式资产
- 生产 split 部署真正生效的是 VPS 上挂载目录，例如 `/opt/china-models/compose/data/`
- 正式静态资源放置原则
  - 客户端正式更新与脚本市场通过 `console` 管理后台页面维护，并写入运行时 `data/public/downloads/`
  - Skills Pack 正式分发优先通过管理后台上传与配置，不把 zip/manifest 作为 `console` 仓库长期发布资产维护

## 仓库与发布

远程仓库：

- `origin` -> `https://github.com/NearZeroOPC/china-models-console.git`
- `legacy-origin` -> 历史旧仓库

普通提交仍会跑后端/安全类检查，但镜像发布类 Action 已调整为仅在 release 时触发。

## 维护提示

- 这里仍然是业务核心仓，改动前要先判断是否真的属于后台职责。
- 客户端或官网相关实现不要反向塞回本仓库。
- 当前工作区里可能残留一些本地调试目录或大文件，不要无差别提交。

参考来源：

- [lieeew/sub2api](https://github.com/lieeew/sub2api)
