# Steadflow Console Agent Guide

这是 `Steadflow` 的模型网关核心服务、用户控制台及核心服务管理后台。`china-models` 仅作为域名和技术兼容名称。新会话如果处理账户、订阅、API Key、渠道、模型网关、分销商或核心服务部署问题，默认先看这里。

根目录下唯一的 Sub2API 派生源码是本目录 `console/`。运行时仍使用 `sub2api` 作为 Go module、容器、镜像和 database 名称，但不得据此寻找或创建平级的 `../sub2api/` 源码目录。

## 业务口径

`console` 是帮助用户使用 AI / Codex 的业务中枢，不是单纯的中转面板。账户、订阅、API Key、数字产品权益、分销商、公开设置和下载分发，都应围绕“让用户先把 Codex 用起来，并继续获得 Skills / workflow / 教程等专业能力”来设计。

商业化产品包括订阅代充、Codex 中转、Codex 付费教程和付费 Skills/workflow/Agents。ChatGPT 账号不属于业务范围，不得新增账号售卖、账号库存或账号履约模型。科学上网只允许作为教程内容，不建设节点、线路或账号售卖能力。

## 这里负责什么

- 后端接口
- 数据模型、数据库迁移
- 用户控制台与账户、渠道、网关等核心服务管理后台前端
- 分销商绑定、佣金、站长 API Key、履约回调
- 公开设置接口
- 客户端依赖的 Skills catalog / manifest / 下载分发链路
- Docker 镜像运行入口

## 这里不负责什么

- 官网与帮助中心：去 `../site`
- 新版商城前端、游客下单页面：去 `../site`
- 新版商城 BFF、游客订单、支付和邮件：去 `../site-bff`
- 新版商城运营后台、订单履约、库存、财务、FAQ/教程和站点发布：去 `../site-admin`（其服务端逻辑在 `../site-bff`）
- 桌面客户端 UI / Tauri / 安装器：去 `../app`
- 营销文档与内容计划：去 `../marketing`
- Skills Pack 源码与打包脚本：去 `../skills`

## 进入本仓库后的优先检查点

- 后端问题：先看 `backend/`
- 管理后台问题：先看 `frontend/`
- 本地 Docker / Caddy / 下载目录问题：先看仓库根目录 `../deploy/`
- 客户端拿不到 skills / settings / manifest：
  - 本地联调先看 `data/public/downloads/`
  - 正式脚本市场与正式更新元数据优先看运行时 `data/public/downloads/` 是否由后台页面正确生成

## 关键联动提醒

- 修改公开配置字段时，要同步考虑 `../app` 的解析与展示逻辑。
- 修改 distributor、API Key 履约或订单回调时，要同步考虑 `../site-bff` 的调用和验签逻辑。
- 修改 Skills Pack 后台配置结构时，要同步考虑 `../skills` 的打包产物格式。
- 修改下载地址、下载路径、反向代理或缓存规则时，要同步验证客户端安装链路。
- 不要把仓库文件误当成“提交后会自动随镜像上线”的正式运行时资源；线上生效的是容器挂载的数据目录和后台写入的数据。
- `site-admin` 不应直接调用本服务；商城运营调用链固定为 `site-admin -> site-bff -> console distributor API`。
- `console/frontend/src/views/admin` 中只新增账户、渠道、模型网关和核心服务运维能力。商城订单、库存、内容和支付通知 UI 一律进入 `site-admin/`。

## 工作约束

- 优先在 `backend/` 和 `frontend/` 中做实现，不要把业务逻辑散落到杂项脚本目录。
- 涉及部署或下载目录行为时，先区分：
  - 本地运行期：`data/`
  - 正式客户端更新 / 脚本市场：优先后台页面生成的运行时 `data/public/downloads/`
- 当前仓库可能存在本地调试残留，如 `.agents/`、`Users/`、二进制产物；除非明确需要，否则不要提交。
- 大文件和构建产物提交前要二次确认。

## 上游升级长期入口

- 官方 `Wei-Shaw/sub2api` Release 只能按 [`docs/UPSTREAM_SYNC.md`](docs/UPSTREAM_SYNC.md) 在 `pipidoudou/sub2api-steadflow` 中生成、验证和审查升级候选；本 fork 是 console 的唯一编辑入口。
- 当前契约只读校验：`./tools/upstream-sync/upgrade --verify-current`；创建候选：`./tools/upstream-sync/upgrade vX.Y.Z`；恢复候选：`./tools/upstream-sync/upgrade --continue`；关键门槛：`make test-steadflow-critical`。
- 冲突只能人工处理 `.steadflow/customization.yml` 已登记的 `shared_seams`。不得修改或删除历史 migration，不得用 force-push、移动 tag 或 squash 合并破坏双祖先历史。
- `upgrade` 不执行 push、PR 合并、tag、Provider/支付调用或部署。只读 CI 不接触生产 Secret；`china-models` 只从后续批准的不可变 `steadflow-vX.Y.Z-rN` tag 做 subtree 同步，生产部署另需 Plan 2 人工批准。
