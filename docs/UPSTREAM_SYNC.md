# Steadflow 上游同步与候选发布

本文档是 `pipidoudou/sub2api-steadflow` 跟进官方 `Wei-Shaw/sub2api` Release 的唯一操作入口。升级候选只在本 fork 中产生和审查；`china-models` 不直接编辑 console，只能在后续独立流程中从不可变的 Steadflow tag 做 subtree 同步。

## 安全边界

- 升级工具只读取官方 tag、创建隔离 worktree/分支、验证并生成报告；它不会 push、创建或合并 PR、创建 tag、部署，也不会调用 Provider、支付或订单履约。
- 禁止 force-push。Task 2 的一次性 fork 初始化已经结束，此后必须保留可审计祖先。
- 候选 PR CI 只有 `contents: read`，不读取 Secret，不注入 Provider、支付或生产环境凭据。只有不可变 `steadflow-vX.Y.Z-rN` tag 会触发镜像发布与 provenance attestation。
- 部署属于 Plan 2，必须另行取得人工批准；升级 PR 或 tag 不代表已经部署。

## 1. 核验官方 Release 与 tag

在 clean 的 fork `steadflow/main` 上先确认 GitHub Release 确实来自官方仓库，记录 Release URL、tag、tag object 和 peeled commit：

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

发生冲突时保留现场。`.steadflow/customization.yml` schema v2 为每个 `shared_seams` 条目登记 `owner`、`risk`、`strategy` 与必跑测试；该清单是唯一来源，不要在文档中维护第二份可能漂移的路径副本。工具以 `rerere.enabled=true`、`rerere.autoupdate=false` 执行合并：历史解决方案可以回填工作区，但不会在无人复核时直接进入 index。

冲突策略语义如下：

- `take-upstream` / `take-steadflow`：已审查的确定性所有权策略，工具可直接解决并 stage。
- `regenerate`：其余语义冲突解决后，从已登记生成命令重新生成并 stage。
- `semantic-resolver`：优先复用 rerere；没有历史记录时保留给人工语义合并。
- `manual`：始终由人工处理。

若冲突路径不在登记的 shared seams、无法同时保持上游和 Steadflow 契约，或需要扩大所有权清单，停止并先审查设计，不要猜测解决。检查工具已应用的策略或 rerere 建议，完成剩余语义合并并按需 stage，再回到 fork 根目录继续：

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

### 已被正式版本取代的过期状态

若旧运行停在冲突或验证阶段，但后续正式版本已经包含旧运行的 Steadflow source 和官方 release 两条祖先，可显式结束旧状态。先根据已审核的不可变注释标签核对完整 commit SHA，再运行：

```bash
./tools/upstream-sync/upgrade --supersede-with steadflow-vX.Y.Z-rN \
  --expected-commit <完整的40位已审核commit_SHA>
```

标签必须严格晚于旧运行的上游版本、等于当前 baseline，且被当前 clean HEAD 包含；轻量标签、SHA 不符、同版本、缺失两条祖先或不完整成功报告均阻断。工具在自行创建并清理的临时 detached worktree 中验证正式标签的当前契约，并在报告记录的历史 candidate 上复核完整成功报告及双格式 canonical 内容；这是读取已有审核证据，不重新执行产品测试，也不宣称后续修订已重新回归。

所有状态操作持有原有 common-dir 锁。旧 `state.json` 原始字节原子归档为 `superseded-vX.Y.Z.json`，独立的 `superseded-vX.Y.Z.receipt.json` 保留原状态 SHA256、正式 tag object/commit、当前 HEAD 和报告 SHA256；两者均为 `0600`。receipt 先持久化，若在最终改名前中断，可在相同证据下重试；不同 receipt 或已有归档不会覆盖。支持从同一 common-dir 的另一 clean checkout 恢复，旧候选路径仍须属于同 common-dir 中原分支匹配的已注册源 checkout 的规范 `.worktrees` 路径；源目录也已丢失时可使用其尚未清理的 prunable 注册证明归属。旧 worktree 即使丢失也不阻断这条审核恢复路径，现存旧 worktree、分支和内部 ref 全部保留。该操作不将旧候选伪标记为验证通过，也不做 push、tag 或部署。

## 3. Known failures 与迁移停止条件

营销与通用 Agent Skills 不属于 Sub2API 运行时定制面，源码统一维护在 `china-models/marketing/vendor/marketingskills/skills/`。fork 中禁止重新引入 `.agents/skills/`；这能避免每次上游升级都携带数百个与 console 无关的差异文件。

认证版本和 exact 失败清单以当前 `.steadflow/known-failures.yml` 的 `baseline.release` 与登记条目为准，并必须与 `.steadflow/upstream-lock.json` 一致。历史报告中的失败数量不能代替当前认证结果。

以后只有可复现的上游非关键失败才能登记。每项必须包含排序且唯一的 exact ID、`category: upstream-known`、原因、`first_seen`、`expires` 和可复现的 `evidence_command`。`expires: vX.Y.Z` 是排他的 release 边界：目标 release 达到或超过该版本时条目过期并阻断。若 exact ID 已通过，则它属于 `fixed`，必须从清单移除后候选才能通过。新增、过期、fixed 未移除及所有 critical 失败一律阻断；critical 没有豁免清单。

`backend/migrations/` 的历史文件受 `.steadflow/migration-checksums.json` 保护：不得修改、重命名或删除。新增迁移必须分类和审查；发现删除/重命名字段、破坏性类型或语义变更、不可逆数据转换等 destructive migration 时，立即停止普通升级路径。只有完成精确 SQL 审查，并使用完整备份演练恢复且验证通过后，才可通过现有 `reviewed_additions` 按文件精确 SHA256 登记审核结果；不得修改迁移内容或扩大工具豁免。部署时必须同时具备与该迁移匹配的恢复策略，镜像回滚不能代替数据库恢复。

## 4. 报告、PR 与不可变标签

候选验证会先在隔离 worktree 内执行 `pnpm install --frozen-lockfile`，再运行完整产品回归，避免依赖主工作树的 `node_modules`。验证完成后，检查 JSON/Markdown 完整报告、候选 commit/tree、官方 tag/commit、生成代码、迁移、known failures、关键命令和完整测试结果。然后由人工把 `upgrade/vX.Y.Z` 推到 fork 并创建 PR；PR 以 `.github/workflows/steadflow-upstream-ci.yml` 的只读结果为合并门槛。同步工具自身的完整用例仅在 `tools/upstream-sync/**` 或其 CI 定义变化时运行；普通上游版本候选仍执行完整产品回归，但不会重复测试同步工具实现。

合并必须保留当前 Steadflow 祖先和官方 release 祖先，禁止 squash 或 rebase 成单祖先历史。合并通过且经人工批准后，在 fork `steadflow/main` 的精确合并 commit 上创建不可变标签：

```text
steadflow-vX.Y.Z-rN
```

同一上游版本的修订递增 `rN`。绝不移动、覆盖或复用旧 tag。合并后再次运行 `./tools/upstream-sync/upgrade --continue` 可在确认候选已成为当前分支祖先后归档完成状态。

不可变 tag 触发 `.github/workflows/publish-reviewed-image.yml`，只做轻量源码身份验证并构建一次镜像，发布 `source-<steadflow_commit>` tag 和 provenance attestation。对于工作流建立前已经审核的 tag，可以通过 `workflow_dispatch` 指定精确 `steadflow-v*-r*` tag 回填同一不可变镜像；工作流会重新核对 tag 与检出提交的绑定。`china-models` 后续只允许验证并晋级该精确镜像，不再重新构建 console。

到此只完成 fork 发布证据。`china-models` 的 console subtree 同步、镜像发布和生产部署属于后续独立流程；任何部署仍需 Plan 2 的单独人工批准。
