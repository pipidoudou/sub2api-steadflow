# Steadflow 上游同步与候选发布

本文档是 `pipidoudou/sub2api-steadflow` 跟进官方 `Wei-Shaw/sub2api` Release 的唯一操作入口。升级候选只在本 fork 中产生和审查；`china-models` 不直接编辑 console，只能在后续独立流程中从不可变的 Steadflow tag 做 subtree 同步。

## 安全边界

- 升级工具只读取官方 tag、创建隔离 worktree/分支、验证并生成报告；它不会 push、创建或合并 PR、创建 tag、部署，也不会调用 Provider、支付或订单履约。
- 禁止 force-push。Task 2 的一次性 fork 初始化已经结束，此后必须保留可审计祖先。
- CI 只有 `contents: read`，不读取 Secret，不注入 Provider、支付、生产环境或仓库写凭据，不登录 registry，也不发布镜像。
- 部署属于 Plan 2，必须另行取得人工批准；升级 PR 或 tag 不代表已经部署。

## 1. 核验官方 Release 与 tag

在 clean 的 fork `main` 上先确认 GitHub Release 确实来自官方仓库，记录 Release URL、tag、tag object 和 peeled commit：

```bash
gh release view vX.Y.Z --repo Wei-Shaw/sub2api \
  --json tagName,isDraft,isPrerelease,url
git ls-remote --tags upstream \
  'refs/tags/vX.Y.Z' 'refs/tags/vX.Y.Z^{}'
git status --short --branch
./tools/upstream-sync/upgrade --verify-current
```

草稿、预发布（除非本次明确批准）、缺失 tag，或 Release 与 tag 不一致时立即停止。`upgrade` 会把远端精确 tag 拉到内部 `refs/steadflow-upstream/releases/vX.Y.Z`，固定 tag object/peeled commit，并在恢复时重新核对；上游 tag 被移动时会 fail closed。

## 2. 创建或恢复候选

```bash
./tools/upstream-sync/upgrade vX.Y.Z
```

工具从当前 clean 分支创建 `upgrade/vX.Y.Z` 和隔离 worktree。无冲突时会完成验证并将脱敏报告写入候选分支：

- `.steadflow/reports/vX.Y.Z.json`
- `.steadflow/reports/vX.Y.Z.md`

发生冲突时保留现场。只允许人工处理 `.steadflow/customization.yml` 当前版本精确登记的 `shared_seams`；该清单是唯一来源，包含核心 wiring/router，以及已经过审查的 CI、文档、版本、生成代码、设置解析和已迁出部署文件边界。不要在文档中维护第二份可能漂移的路径副本。

若冲突路径不在登记的 shared seams、无法同时保持上游和 Steadflow 契约，或需要扩大所有权清单，停止并先审查设计，不要猜测解决。解决已登记冲突后在候选 worktree 中 stage 解决结果，再回到 fork 根目录继续：

```bash
./tools/upstream-sync/upgrade --continue
```

不要手工删除 upgrade 分支、worktree、内部 ref 或状态文件。异常中断后先只读检查：

```bash
git worktree list --porcelain
git rev-parse --git-common-dir
cat "$(git rev-parse --git-common-dir)/steadflow-upstream-sync/state.json"
```

状态中的 `phase` 为 `merging`/`conflicted` 时处理已登记冲突后运行 `--continue`；`validating` 时直接运行 `--continue` 复用现有候选；`merged` 时核对候选 HEAD、clean worktree 和两份报告。报告或候选现场必须保留，不能用重新执行真实业务或连续重试代替诊断。

## 3. Known failures 与迁移停止条件

当前 `.steadflow/known-failures.yml` 的认证 `v0.1.178` 基线为 0 个失败；早先提到的 12 个失败未在认证环境复现，不能据此声称它们已被逐项修复。

以后只有可复现的上游非关键失败才能登记。每项必须包含排序且唯一的 exact ID、`category: upstream-known`、原因、`first_seen`、`expires` 和可复现的 `evidence_command`。`expires: vX.Y.Z` 是排他的 release 边界：目标 release 达到或超过该版本时条目过期并阻断。若 exact ID 已通过，则它属于 `fixed`，必须从清单移除后候选才能通过。新增、过期、fixed 未移除及所有 critical 失败一律阻断；critical 没有豁免清单。

`backend/migrations/` 的历史文件受 `.steadflow/migration-checksums.json` 保护：不得修改、重命名或删除。新增迁移必须分类和审查；发现删除/重命名字段、破坏性类型或语义变更、不可逆数据转换等 destructive migration 时立即停止，不能把它混入普通上游升级。

## 4. 报告、PR 与不可变标签

候选验证完成后，检查 JSON/Markdown 完整报告、候选 commit/tree、官方 tag/commit、生成代码、迁移、known failures、关键命令和完整测试结果。然后由人工把 `upgrade/vX.Y.Z` 推到 fork 并创建 PR；PR 以 `.github/workflows/steadflow-upstream-ci.yml` 的只读结果为合并门槛。

合并必须保留当前 Steadflow 祖先和官方 release 祖先，禁止 squash 或 rebase 成单祖先历史。合并通过且经人工批准后，在 fork `main` 的精确合并 commit 上创建不可变标签：

```text
steadflow-vX.Y.Z-rN
```

同一上游版本的修订递增 `rN`。绝不移动、覆盖或复用旧 tag。合并后再次运行 `./tools/upstream-sync/upgrade --continue` 可在确认候选已成为当前分支祖先后归档完成状态。

到此只完成 fork 发布证据。`china-models` 的 console subtree 同步、镜像发布和生产部署属于后续独立流程；任何部署仍需 Plan 2 的单独人工批准。
