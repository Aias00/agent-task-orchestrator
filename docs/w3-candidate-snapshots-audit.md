# W3：四个未合并候选快照的源码审计与处置建议

状态：**文档审计已完成，四项处置定性已获评审批准（未批准任何运行态改动或移植实施）**。本轮只读比对源码、导出声明和历史资格说明；没有运行快照代码、历史验收器、真实记录探针或模型，没有移植功能、安装入口或修改运行态。以下执行顺序仅供参考，具体后续研发优先级以实际立项决定为准，不构成实施授权。

## 1. 固定审计对象与证据边界

- 对照基线：`main@0f4512552329ce3041b19dd717f6e9438d763af6`（下称 main），本审计分支由它创建。
- 快照来源：本地远端跟踪引用 `origin/work/local-candidate-snapshots-20261120`，解析为 `7ec8221c08c55cdee85f5b9767e66fcd53a47876`（下称 S）。本文所有快照链接固定到 S，避免分支日后移动改变结论。
- 本次尝试 `git ls-remote origin refs/heads/work/local-candidate-snapshots-20261120`，因本机 GitHub 凭据不可用失败。**未确认实时远端头**；审计覆盖现有完整远端跟踪提交 S，不冒称完成在线同步。没有为此读取凭据或改认证配置。
- 从 Git 对象读取 `snapshots/` 共 614 个文件：四目录 613 个文件，加总索引 1 个。源码副本只在仓库外供静态分析，没有加入本分支或 main。没有读取被排除的原任务、SQLite、原生会话正文、auth 或私有宿主证据。
- 对各 `SOURCE-SNAPSHOT.json` 的 `published_source_sha256`，按 `original_to_published_paths` 逐一核对实际文件，四项均无不匹配。`README.md → README.original.md` 是导出映射，不能把包装 README 当原设计。`source_sha256` 记录原文件，`adapted_files` 记录发布时脱敏，**导出文件哈希相符不等于原始宿主验收通过**。
- 四份 manifest 均声明 `stable_release=false`、`original_frozen_acceptance_reissued=false`。`OPERATOR_*` 是未解析占位符；被排除的 baseline/proof/log/归档不能从当前公开源码补造。历史文档中的“qualified”、旧测试数及操作示例必须按各自版本/阶段理解。

本文用“已覆盖”“部分覆盖”“真实缺失”区分能力，不以同名文件缺失直接推断功能缺失。源码依据是 S 的真实实现与 main；历史记忆仅作查找线索，不替代以下证据。完整来源清单和模块清单见第 8 节。

## 2. 总处置表与执行顺序

| 快照 | 建议（三选一） | main 真实缺失的主要增量 | 最小后续范围与停点 |
|---|---|---|---|
| `tracking-rectification` | **INCREMENTAL_PORT** | accepted question→独立授权交付的观察关系；精确 legacy workspace 关联；跨 workflow 同一实际交付去重；任务级责任/内部材料 read/assign 回执 | 先移植**观察关系的校验规则与定向用例**，经新叶模块/注入读取边界重新集成；不带旧 Runner/controller、旧存储或旧历史注册。第一增量只给可验证投影，不承诺修复全部跟踪目标 |
| `execution-efficiency` | **REDESIGN_FROM_MAIN** | 跨入口的任务运行版本绑定、细分计账/容量、精确 worker 连续上下文、增量验收缓存/检查点、分项指标 | 放弃旧运行实现的整体移植，在 main 设计独立策略命名与凭据/版本/验收边界；旧算法和故障用例仅作为设计输入。先版本绑定与可观测性，后复用与性能 |
| `agy-acpx-integration` | **REDESIGN_FROM_MAIN** | 面向外部协调者的精确合约能力 CLI；ATO-owned `acpx_codex` worker、固定协议 parser/gateway、AGY 完整编码交付 | 在现行 registry/session/CLI/工具链边界重设计；先分离通用 external scope 与 acpx worker 两条资格链，真实编码/新增入口须另行授权 |
| `legacy-managed-mailbox` | **ARCHIVE_ONLY** | 旧 Claude 托管原生会话实现仍非 main 已资格能力，但其旧协议/证据不足以构成当前可直接移植增量 | 建议正式保留为只读历史档案，**不再计划合入这份旧代码**；历史 native 证据不重签，不删除分支，不把旧迁移/restore 工具作为现行发布工具 |

推荐顺序：

1. 用户评审本表后，先确认 `legacy-managed-mailbox` 的档案定性，只更新状态说明，不做分支清理或运行态操作。
2. 对 tracking 开一个独立小增量：accepted-delivery 只读关系校验与投影，精确保留未覆盖的旧验收。完成独立离线负例后，再评审 workspace、same-operation 去重、内部回执/责任四个子问题；不要一口气合入六模块及其 controller 改动。
3. 从 main 另立 efficiency 设计：先明确策略命名冲突、任务级版本绑定及分项测量；warm resume、缓存、容量分别验收。跨软期限的细粒度竞态可单列确定性缺陷评审，不必等待整个效率项目，也不能在本轮顺手改代码。
4. AGY/acpx 在上述基础边界明确后另立设计。通用 external scope 的离线设计可以独立推进，但模型试跑、安装和用户项目接入不是 W3 的下一自动步骤。

三项研发建议均应先获得用户对具体范围的决定；本轮结束于审计文档提交，不恢复任何历史任务或试跑额度。

## 3. 共同架构差距：为何不能直接 cherry-pick/覆盖

当前基线以 [ato_core.py](../ato_core.py)、[ato_cli](../ato_cli/parser.py)、[源码图](source-map.md) 分离公共原语、CLI 分发和运行机制。按当前 [check_architecture.py](../scripts/check_architecture.py) 同一 AST 算法（包含函数内 import，仅分析，不 import 快照模块）得到：

| 树 | 全部文件 | manifest 发布源码项 | 根 Python 模块 | `test_*.py` 文件 | 根/全运行图双向依赖对 | 对照 main 的八核心 AST |
|---|---:|---:|---:|---:|---:|---|
| main | 219 个受 Git 管理文件 | 不适用 | 50 | 以全量 800 用例验收为准 | **8 / 8** | 本轮必须全部不变 |
| tracking | 174 | 170 | 41 | 46 | **39 / 39** | 6 个改变，2 个相同 |
| efficiency | 206 | 202 | 45 | 55 | **40 / 40** | 7 个改变，1 个相同 |
| AGY/acpx | 174 | 166 | 38 | 44 | **30 / 30** | 6 个改变，2 个相同 |
| legacy mailbox | 59 | 57 | 15 | 19 | **14 / 14** | 7 个存在的核心均改变，缺 `session_router.py` |

八核心指 `control/recovery/interaction/delegation/workflow/session_router/message_bus/transport.py`。tracking 与 AGY 的变化集合均为 `control/recovery/interaction/workflow/session_router/transport`；efficiency 另改 `delegation`；legacy 改 `control/recovery/interaction/delegation/workflow/message_bus/transport`。这些是规范化 AST 对比，不把尾部空行算功能变化；其中也包含公共原语抽离等结构差异，不能将“改变”全部归为候选特性。

四份树均缺 `ato_core.py`、`ato_cli/`、`pyproject.toml`、`pi_toolchain.py`、`pi_acp_compat.py` 和 `scripts/release_snapshot.py`。旧 `runner as core` 反向依赖、内联 argparse、旧 Pi 派生和脱敏后的 operator locator 都需要拆解；移入 main 会丢失 Batch 1–3、P3-1、W1 和 W2-1 边界，不能靠删除旧图里的边来“过门禁”。

典型新增循环（相对历史 31 对基线）：tracking 的 `approved_delivery↔interaction/runner`、`completion_delivery↔runner`、`runner↔workspace_association/task_responsibility`；efficiency 的 `runner↔runtime_selection/worker_sessions/verification_cache/incremental_checkpoint` 和 `agent_registry↔runtime_selection`；AGY 的 `external_coordinator↔local_session_adapter`、`acpx_worker↔agents`；legacy 的 `mailbox_routes↔runner/recovery/interaction/delegation`。**即使 AGY 的 30 小于历史 31，也不满足当前 ≤8 且旧基线子集要求。**

main 的八对是 `agents–permissions`、`budget–runner`、`control–recovery`、`interaction–notifications`、`recovery–runner`、`recovery–session_router`、`runner–workflow`、`session_router–transport`。本轮没有改这些核心、旧基线或测试。后续增量应使用公共原语、单向 adapter/观察边界及通用 callback；不得让核心重新解析 vendor、原生认证/事件，也不得让 Batch 2 的被动 store 承担新的策略或执行决策。

按现行 [通用 vendor AST 门禁](../tests/test_pi_production_profile.py#L153) 的导入前缀检查，tracking、efficiency、AGY 的八核心没有直接 vendor import 命中；这不等于它们与当前核心 AST 相同或满足依赖图门禁。legacy 的 [delegation.py 第 10 行][L-core] 直接 `import appserver`，还需要单独消除 vendor 侵入。不能把所有源码漂移一概称为 vendor 污染，也不能把“未命中直接 import”当成可移植资格。

发布能力也必须分开判断：

| 当前 main 机制 | 已覆盖范围 | 不能据此宣称已覆盖 |
|---|---|---|
| [W1 release_snapshot](runtime-upgrade.md)、`verify_snapshot`/`inspect`/`idle_boundary` | commit/tree、封存清单、三入口混合诊断、精确 observer/pid_identity、mailbox 门禁、显式切换、仅可执行入口/observer 回滚 | 每个任务跨不同入口重启仍绑定原版本；旧任务迁移、旧快照宿主资格 |
| [P3-1 工具链](p3-1-toolchain-design.md)、[compat](../pi_acp_compat.py) | 固定 Node/Pi/backend、派生输出 SHA、保护启动物和父子复验 | Codex worker warm resume、acpx 依赖资格或 AGY 原生身份 |
| [W2-1 离线实现](w2-1-offline-implementation.md) | 独立 production Pi opt-in、凭据/fence/出口、受控请求与有界原生组清理 | 真实 Provider、native load/resume、通用 Codex/acpx transport 资格 |

旧 packager 的 hash/provenance 只能说明其版本自身；不得将旧 `OPERATOR_*` 换成本机路径后运行，让历史授权或验收“复活”。本轮没有调用任何发布工具的真实 dry-run/stage/activate/rollback。

## 4. tracking-rectification

### 4.1 原始目标与真实源码

[原任务][T-task] 明确包含五项：父 workspace→子 Git 工程的精确 legacy 绑定、已批准交付观察、内部材料回执、workflow 责任/诊断处置、后继同一交付完成通知去重；以已有 591 测试通知控制器为历史种子，并要求保留旧 VPS 未完成项与另行完成的交付，不替旧 efficiency 任务结案。`README.original.md` 是当时通用调度器说明，目标细节应同时读上述任务文档，不能仅据目录名补想象。

174 个实存文件中，manifest 映射 170 项，39 项经发布脱敏；另有包装/清单与 operator 示例。新增于 main 文件面的六模块为 `workspace_association.py`、`approved_delivery.py`、`internal_coordination.py`、`task_responsibility.py`、`responsibility_process.py`、`completion_delivery.py`。相关定向测试为 `test_tracking_workspace_binding.py`、`test_approved_delivery.py`、`test_internal_coordination.py`、`test_tracking_responsibility.py`、`test_completion_lineage_dedup.py`、`test_tracking_candidate.py`；封装/探针为 `prepare_tracking_candidate.py`、`verify_tracking_readonly.py`。全根模块清单见第 8 节。

### 4.2 与 main 逐项对照

| 能力及快照实现事实 | main 对应事实 | 覆盖结论 |
|---|---|---|
| [workspace `_git/_proof/validated`][T-workspace] 只接受所选 workspace 的直接子仓库，绑定原生 header、home、精确 origin/hub/contract/generation、Git inode；拒绝 linked worktree 的无证明扩展，提供 inspect/register/revoke | [Codex `bind_legacy_origin`](../codex_session_adapter.py#L295) 遇已知 cwd≠project 仍拒绝；v2 精确 project 校验不可放宽 | **真实缺失**：显式、限 workflow 的 legacy 观察关联。旧代码本身也不支持任意祖先或任意 linked worktree |
| [approved `_base/_authority_delta/_projection`][T-approved] 校验已实际 accepted 的问题/answer source、独立 delivery 合约、完整证据、实际权限 delta 与 approved/deferred criteria；保持旧 raw status | [workflow_lineage `_validate`](../workflow_lineage.py#L45) 要求 `permission_expanded=false`、权限字段相同、原 acceptance 全量映射；closure 是另一种有真实依据的处置 | **部分覆盖**：一般同权限后继和关闭已覆盖；“同一 accepted 批准下另行扩权交付、仍保留旧残余”的关系真实缺失，不能伪装成普通 lineage |
| [internal `submit/read_report/assign`][T-internal] 把安全 artifact/hash 与规范 mailbox 绑定，区分提交、读取、有限认领；`inbox` 明报 `wakeup=unsupported` | [coordination](../coordination.py) 和 [f84fe37 failure_notice](../failure_notice.py) 已将宿主/失败长材料私存，发送紧凑 locator；canonical 问答/actor/mailbox 已有 | **部分覆盖**：紧凑通知和基础传递不再是增量；任意内部材料的 read/assign 工作流回执仍缺。旧实现也没有“自动隐形唤醒” |
| [responsibility `claim/handler/dispose/diagnostic`][T-responsibility] 绑定 failure episode、证据、角色与 ≤900 秒认领，不把聊天忙碌当任务处理；区分 disposition 与业务恢复 | [recovery `handler_status`](../recovery.py#L313) 已辨 owner/worker/recovery/unknown，但普通 origin active 仍只给会话级处理分类；failure_attention 有稳定失败 episode 用于通知 | **部分覆盖**：基础故障/once 通知已有；任务级有界认领、处置凭据、具体 reason code 与责任投影仍缺 |
| [completion `identity/_operation`][T-completion] 从兼容 lineage 或双方原有共享 operation receipt 计算同一次交付身份；未知旧发送 claim 是去重屏障 | [completion_notifications](../completion_notifications.py) 是按 `workflow_result`/canonical claim 的 once；可验证同权限 lineage 存在，但没有跨 workflow 的同一物理交付 identity 账 | **真实缺失**：跨后继去重身份。不能由“同仓库/相同测试结果/类似文案”推出同一交付 |

`approved_delivery` 和 `completion_delivery` 是两种关系：前者承认独立授权下某 approved scope 已交付，后者证明两个完成记录对应同一次实际 operation；二者不能互相代替。`task_responsibility.py` 与 efficiency 有重叠，也不能双份导入。

### 4.3 架构、资格差距与处置

直接移植成本为**中高**：39 对循环、六核心漂移，观察校验与现有 interaction/controller/runner 相互 import；缺现行 P3-1/W2-1 和通用模块分发。最小复用范围是 `_authority_delta`、accepted-answer/criterion/proof 校验、只读投影及其攻击/漂移用例，改为向新观察叶模块注入 canonical 读取接口。不要复制旧 runner、CLI parser、controller 状态表或依赖 `runner as core` 的整模块。

导出状态为 **Source fixture candidate qualified; real-record historical linkage gate incomplete**。原 [宿主交接][T-host] 要求真实旧记录的既存兼容 lineage 或双方共有的既存 operation receipt；[真实记录探针][T-probe] 在缺该证明时明确拒绝。已导出的源码没有补齐私有历史证据，也没有重签原冻结验收。历史 worker 验证文档还保留过 499 项中 488 通过、9 失败、2 错误的部分结果，不能拿它或本轮 main 的 800 项代替最终宿主资格。

建议 **INCREMENTAL_PORT**，独立验收边界：

1. 第一增量仅实现 accepted-delivery 的证据校验和残余投影；覆盖真 accepted vs 仅 answered/queued、权限 delta、缺/漂移证据、manual acknowledgement、环/冲突、未获覆盖旧 criterion。输入显式注入，拒绝自然语言推断批准；raw state/contract/STOP/attempt 不改。main 全量、≤8 旧对集合及 vendor AST 门禁保持。
2. workspace、same-operation 和 internal/责任各自另验：错误 home/header/project、symlink/linked-worktree、不足的历史 receipt、unknown no-replay、认领过期/不续期、artifact 漂移、读取不等于处理。先用合成记录；真实记录核对必须另获明确范围，不扫描线上历史来寻找“能过的证明”。
3. 若接入当前 controller 必须新增核心依赖、改变总账语义或放宽历史门禁，停止该小移植并另立设计；不以 INCREMENTAL_PORT 为理由承诺旧五项目标或历史案例已完成。

这里的第一切片是独立可验收的观察校验器。若后续目标指定为原父 workspace/子仓库案例，**精确 workspace 关联是其现有 binding/read-question 路径的前置条件**，须先单独评审接入；不得为让 accepted-delivery 投影先工作而绕过 main 的 project 校验。shared-operation 历史证明又是跨 workflow 去重的独立前置，三个条件不能互相代替。

## 5. execution-efficiency

### 5.1 原始目标与真实源码

[原任务][E-task] 继承 Linger677 历史前驱，组合“固定运行版本、任务责任、专属 worker 连续轮次、增量检查点、按输入复用验收、准确计时及真实三组对照”。`README.original.md` 第 320 行起实际写有 v2 continuation accounting、task_view、容量、verification 与整体效率候选；不是一个单纯缓存库。

206 个文件、manifest 202 项、52 项脱敏；11 个 main 不存在的根模块为 `runtime_selection.py`、`execution_accounting.py`、`capacity.py`、`task_presentation.py`、`task_responsibility.py`、`worker_sessions.py`、`codex_worker_session.py`、`incremental_checkpoint.py`、`verification_cache.py`、`execution_metrics.py`、`profile_evidence.py`。配套有 worker reuse、incremental verification/checkpoint、capacity queue、task summary、profile drift、deadline/timeout 等 16 个 main 不存在的测试文件，以及 fixture benchmark、actual engine、两种 candidate packager；完整旧测试文件数为 55。

### 5.2 与 main 逐项对照

| 能力及快照事实 | main 对应事实 | 覆盖结论 |
|---|---|---|
| [runtime `pin/load/route`][E-runtime] 为新任务/父子阶段记录可信 runner/python/source 和外部 receipt；从另一入口启动仍转回绑定版本，历史无绑定明确 legacy | main `runtime_command` 固定**当前进程**的 runner/interpreter；[release_snapshot](runtime-upgrade.md) 封存/切换运行目录，文档明确任务级 pinning 仍属旧候选 | **部分覆盖**：源码封存/进程选择已有；持久化的逐任务版本绑定、跨入口重启及 parent 传递仍缺 |
| [accounting `validate/begin/finish`][E-accounting] 使用 `execution_policy.version=2` 区分 invocation/continue/wait/yield/failure，幂等终态记账 | main budget/advisory、累计 attempts 和有限重试存在；[bee82ee execution_policy](../execution_policy.py) 同名字段现在表示副作用 `source/instructions/actions/required_actions`，拒绝 version/max_continuations 等 | **真实缺失且协议冲突**：不能把旧 accounting policy 当新版 execution_policy。必须另命名并设计兼容，绝不可覆盖 worker 动作归属策略 |
| `capacity.py` durable endpoint admission，interactive reserve、无抢占、无 TTL 回收，精确 receipt+confirmed cleanup 才释放 | main 仓库 flock、execution lease、mailbox 单 turn 槽已防并发；没有跨项目 endpoint 容量池 | **部分覆盖**：执行独占不是 endpoint 配额；容量/优先级/饥饿投影仍缺，不能用新队列替换现有 owner |
| [worker `preflight/execute`][E-worker] 绑定任务、profile、native/turn，unknown 隔离；`codex_worker_session.py` 按精确 ID `exec resume`，每 turn 仍重启 CLI | main 有 registry/session capability、Pi 协调端与显式 checkpoint reconstruction；默认 Codex worker 仍按轮调用，W2-1 生产 Pi load/resume unsupported | **真实缺失**：任务专属 Codex warm context；旧 capability 文本也承认安装 CLI 尚未 origin-qualified，不是常驻进程或通用 native resume |
| [checkpoint `publish/preflight`][E-checkpoint] 保存 host-validated 文件/check/native/profile/counters，放在 worker grant 外；[cache `fingerprint/lookup`][E-cache] 仅支持严格识别的 Python argv、声明输入/依赖/toolchain/env、完整证据 | main 有 worker输出 checkpoint、canonical checks/evidence、独立最终复查；没有 verification_policy/cache/checkpoint_policy 入口 | **部分覆盖**：安全基线已有，宿主可验证增量 receipt 和有限缓存仍缺。快照会对未知依赖走原 argv uncached，final/full/manual/external 必须 fresh，不是任意命令缓存 |
| [metrics `project`][E-metrics] 对闭区间取并集，最深 phase 归类，并发/缺数据显式 unknown；profile_evidence 仅 bounded labels/hash/enums | main 有 [task_trace](../task_trace.py)、运行事件和预算时间，无同等阶段重叠分摊与 profile 字段差异协议 | **部分覆盖**：日志与累计时间已有；可比较的分项指标仍缺。CLI command 时间不能冒充模型/网络时间 |
| [延迟边界补救][E-remediation] 在 enforcement 时再 `budget.poll(force=True)`，另修 v2 无 checkpoint_policy 时 timeout 的 failure 记账 | main [W1 Runner](../runner.py#L105) 已用 ≤150ms exit-aware wait，[budget.poll](../budget.py#L384) 已在 soft 边界绕过周期节流；但 guard 仍在 poll 后另读时钟，没有 force 参数/二次重检 | **部分覆盖**：W1 已替代旧固定睡眠及主要 polling 缺陷；“poll 后跨界”的更细竞态仍是可独立评审差异。v2 failure 记账依赖旧 accounting，不能直接套 main |
| task_view/责任闭环展示 observed/raw/full goal、进程/剩余验收/认领，查询不启动 work | main status/list 已有 overall/stage、observed/effective status、remaining/recovery/独立 lineage；handler 仍非任务级认领 | **部分覆盖**：基础投影已覆盖；合并容量、细账、任务级责任的完整只读视图仍缺，依赖前述数据协议 |

### 5.3 架构、资格差距与处置

直接移植成本为**高**：40 对循环、七核心漂移；旧 `_sources()` 枚举以根模块/旧目录结构为主，未设计 `ato_cli` 和现行封存清单，不能原样给 main 发运行版本证明。旧 Codex launcher 的固定 digest、config/profile 观察协议也不是 P3-1/Pi 的可移植替代。缓存与检查点按旧 plan/worker 权限模型实现，必须重新核对 bee82ee 动作权限和 W2-1 秘密/unknown 费用，不能用缓存 receipt 绕过新 guard。

导出状态为 **Active source requalification; actual native benchmark incomplete; not a deployable release**。旧 677 测试前驱资格只属于旧 bytes；[validation][E-validation] 保留原 936 测试两失败、后续 953/955 发现数与受限 worker 的失败/错误，不能选最大测试数当“已通过”。真实 foreground/old/new ATO 三组样本首轮因 `native profile changed during turn` 失败，其 cause 仍 unknown；新增 hash-only 诊断复现的是一类 drift，**没有证明旧故障根因**。[continuation][E-drift] 要求保留 per-manifest exclusive claim、旧失败 journal，当前 successor 明确禁止 `--run-origin`，真实 benchmark 仍未完成。本文未重新采样，也不把 fixture benchmark 当云端提速。

建议 **REDESIGN_FROM_MAIN**：不移植旧 Runner/Workflow/Controller 或其 execution_policy v2。先提交新设计，分开版本绑定、只读指标、任务认领、容量、checkpoint/cache、native worker 六条验收；`profile_evidence.snapshot/difference`、`execution_metrics.project` 的纯逻辑与定向故障用例可以作为算法参考，不表示旧模块已获准移植。

独立验收应至少证明：旧任务/bee82ee policy 字节语义保留；不同入口/父子阶段仍使用精确已封存版本且漂移拒绝；每次真正调用仍走原 owner/STOP/cost fence；unknown 不换会话/重放；缓存需完整输入/解释器/argv/env/权限/证据，未知依赖和最终/manual/external 一律 fresh；指标先有可信基准再讨论提速。真实 CLI 对照是未来单独人工授权门槛，不沿用历史额度。

## 6. agy-acpx-integration

### 6.1 原始目标与真实源码

[原任务][A-task] 要求“AGY→ATO 管理任务→acpx/ACP 仅作通信→Codex 编码”，而非让 acpx 成为任务管理器。外部协调者只能操作宿主已批准的精确 repo/contract capability，不能 approve、acknowledge、增 grant 或重绑 hub。`README.original.md` 主要是共用调度器介绍；AGY 专属目标与限制在 `docs/external-coordinator/`。

174 个文件、manifest 166 项、32 项脱敏；新增根 Python 模块 `external_coordinator.py`、`external_operator.py`、`acpx_worker.py`、`acpx_profile.py`，另有 **`acpx_bridge.mjs` 与 `acpx_native_proxy.mjs` 两个实质运行模块**，不能因不在 Python 图中忽略。定向测试为 `test_acpx_worker.py`、`test_external_coordinator.py`、`test_external_coordinator_lifecycle.py`、`test_external_qualification.py`，另有 budget boundary 测试。脚本包括 `prepare_external_candidate.py`、`external_host_smoke.py`、`external_host_readback.py`；5 个 supplemental operator 源文件只是归档源，不能运行其 install/demo 来恢复旧授权。

### 6.2 与 main 逐项对照

| 能力及快照事实 | main 对应事实 | 覆盖结论 |
|---|---|---|
| [external `reviewed_contract/load_scope/submit`][A-external] 强制隔离 homes、canonical repo、固定 stages、memory=false、受限 sandbox；稳定 request hash、unknown creation 不重放；只开放 init/submit/status/events/result/stop/continue | main `local_cli`、v2 actor、workflow-create、canonical questions/STOP/独立检查已经存在 | **部分覆盖**：基础语义已有；对模型暴露的“精确合约能力 CLI”和全生命周期 request scope 仍缺，不能把 local_cli 等同于 AGY 原生身份 |
| `external_operator.provision` 才授予精确 scope；`local_session_adapter` 回调 external scope，形成循环 | main 角色/授权通用接口存在；不存在 external_scope 回调或 ato-control 分发 | **真实缺失**：安全 provision 与模型面 API 的职责分离。需新通用 capability seam，不让基础 adapter 回调具体产品模块 |
| [acpx worker/profile][A-profile] 新 worker kind `acpx_codex`；固定 acpx 0.22.0、codex-acp 1.1.5、Codex 0.144.6、Node24 pin；worker-only，planner/原生 continuation 均不支持，无自动 fallback | main 支持既有 Codex/Claude/Pi worker 与 Pi ACP；P3-1 固定的是 Pi 后端；W2-1 是 production Pi transport | **真实缺失**：acpx_codex worker 和其独立 TCB。Pi 资格不能传递给这条链 |
| [bridge][A-bridge] 借 `onAcpMessage` 以 messageId+Codex phase 只接受精确 final；[proxy][A-proxy] 固定 thread/turn named policy、拒绝审批/用户输入/elicitation，防 legacy sandboxPolicy 覆盖 | main schema/ACP error/unknown/独立 checks 原则已有；没有该版本的 Codex ACP parser/gateway | **部分覆盖**：不变量已有，版本私有 parser/模式回读仍缺，禁止把这些 vendor 规则移入八核心 |
| operator demo/安装工具描述新 `ato-control`/AGY 交付与 greeting.py 检查 | main 没有这条新增用户入口；`ProductionPiProfile` 不代表 AGY coding qualification | **真实缺失**：真实可用编码闭环与受审查新增入口；不是“文档给了命令所以已交付” |

### 6.3 架构、资格差距与处置

直接移植成本为**高**：30 对循环、六核心漂移，缺 P3-1/新 CLI/封存；`acpx_profile.py` 的固定私有依赖/Node/native digest 及 Codex named-policy proxy 是另一套专用信任链。源码还保留一个 `(allow default)` 的旧 `sandbox_profile` helper，而后续 `codex_profile` 改用单一 native named sandbox；不能拿旧 helper 字符串证明有 W2-1 default-deny 等价边界。实际调用链、继承环境、read deny、权限回读、cleanup 必须重新资格化。

导出 manifest 明确 **Software candidate qualified; real demo failed/unknown; extra real trial and actual additive delivery pending**。[阶段 validation][A-validation] 保留 627 测试 11 失败/14 错误、601 worker-feasible 通过但 34 项延后等分层记录；它们不是本轮 current main 的通过数，也不是新的完整 native 资格。后续交接记载过 636 宿主测试及新增 cleanup 测试需求，仍要求绑定改变后的精确源重新验证。真实 coding demo 的失败/unknown、已消耗 model admission 与新增入口交付未完成必须保留。

特别是 [operator demo-README][A-demo] 有“直接试用”和已资格范围的操作口吻，但配套私有 `delivery.json`/安装回执不在导出中，不能推翻 manifest 的导出状态。没有据此核查或安装任何本机入口。[origin handoff][A-host] 的旧 ≤3 次 admission（含意外消耗的历史次数）、单次时限和 smoke 命令只是历史合约，不是 W3 的执行许可。

建议 **REDESIGN_FROM_MAIN**：先在 main 的通用 actor/registry/CLI/owner 边界定义 external-scope 协议，再独立设计固定 acpx worker。可参考 request 幂等、精确 final-message parser、named-policy 防降级负例，但不能照搬私有路径、旧 native pin、全量旧 parser 或 install helper。

独立离线验收边界：scope 不能跨 repo/contract/generation；禁止模型 approve/ack/grant/hub/retry 重置；并发 submit/unknown spawn 无第二 writer；transport 完成不能代替 schema+业务 checks；错误 final/中间 commentary/截断/错 mode/审批/STOP/父 EOF/后代不退均拒绝；所有新源纳入现代分发/封存并保持八核心 vendor-free、≤8 旧对。**真实模型编码试跑、额外 agent、用户项目和入口安装**须后续逐项授权，不因审计或离线全绿自动执行。

## 7. legacy-managed-mailbox

### 7.1 原始目标与真实源码

这是 manifest 标为 **Original dirty maintenance checkout preserved; not selected as stable deployed baseline** 的旧工作树，不能从名称推断它已是某个正式发布版本。`README.original.md` 的 agent-neutral workflow routing 与 offline upgrade 介绍，结合 [message-delivery-validation][L-validation]，实际目标是 canonical mailbox、混合 managed origin/hub、CLI adapters、native protocol、全回归和离线迁移交付七项。

实存 59 文件，manifest 57 项、3 项脱敏、无 supplemental operator 源；根 Python 仅 15 模块，缺独立 registry/session_router 层。核心实现是 `mailbox_routes.py`、`message_adapters.py`、旧 `message_bus.py/transport.py`；`ClaudeAdapter/PiAdapter` 支持精确 ID/pending barrier 的两轮会话思路。三个脚本为 `install.py`、`upgrade_runtime.py`、`verify_native_mailbox.py`；19 个测试文件含 `test_agent_mailbox_integration.py`、`test_message_adapters.py`、`test_message_bus.py` 和 `test_upgrade_runtime.py`。

### 7.2 与 main 逐项对照

| 旧能力与实现事实 | main 覆盖/替代机制 | 结论 |
|---|---|---|
| [mailbox_routes][L-routes] 注册 `managed:claude/pi:UUID`、保存 endpoint capability，旧 JSON 决策账本独立，send/pump/ack/invalidate | [message_bus](../message_bus.py)、[transport](../transport.py)、[session_router](../session_router.py)、[registry](../agent_registry.py) 已分离路由/身份/actor/能力，具备 v2 fencing/租约/unknown 隔离；local_cli 和 Pi managed 另有适配器 | **核心目标已覆盖且结构更清晰**；旧 `origin_endpoint_id`、环境 token/旧 record 格式不是要自动兼容导入的状态 |
| [CodexQueueAdapter/NativeAdapter][L-adapters] exact queue receipt、pending no replay、原生 session state、Claude/Pi 回合与工具约束 | main 已有 Codex adapter、受管 Pi ACP 和 worker，P3-1/W1/W2-1 增加固定工具链/清理/凭据出口；generic Claude coordinator/native resume 未据此获资格 | **部分覆盖**：Claude 管理会话是诚实保留的缺口，但这份旧实现不是已经满足当前 ABI/授权/工具链的增量；需要时另立新设计，不恢复旧环境传参或协议 |
| 旧 mixed-agent tests 与传统 CLI 宣传中的 mailbox-register/mailbox-pump | main 以 session-register/local-session-create/origin-ref 和 v2 actor 分发；Batch 1–3 已纠正主 README 的旧接口 | **已替代**：不能恢复旧命令名来“对齐”历史 README，不能因测试文件改名就说缺了 mixed routing |
| [upgrade_runtime `assess/stage/restore`][L-upgrade] 为明确 offline 临时根作字节快照/restore，sidecar 存在则拒绝，源不含依赖/测试 | [现行 release_snapshot](runtime-upgrade.md) 以 Git commit/tree、封存资格、只读 mailbox/observer 门禁、逐入口备份与可执行路径回滚替代发布；明确不恢复业务/SQLite | **发布目标已替代**：旧状态 restore 不是应补进 release_snapshot 的功能，也不是激活前置；保留作历史离线演练参考 |

### 7.3 架构、资格差距与处置

直接移植价值低、成本**高于增益**：虽只有 14 对循环，仍超当前 8；七个现存核心 AST 均不同，缺 session_router/ato_core/ato_cli/现代元数据/compat，旧 native 细节在 `mailbox_routes` 与运行核心之间穿插。恢复旧模块会反向吞并已拆开的路由层，也会丢掉最新发布和 worker 执行策略。

历史 [validation][L-validation] 只称 deterministic 147/177（基线 90/120）可运行，30 个 socket/nested-sandbox 用例延后；另记旧 Claude 2.1.325/Pi 1.5.2 曾在本地 provider 两个相关回合成功，但**后续 bus/adapter 改动需新宿主 native 资格**，更不证明真实云端账号或当前 source。七个冻结 gate 的私有证据并未随导出重签。目录的“dirty”是原导出事实，不表示本轮要修理或清空它。

建议 **ARCHIVE_ONLY**：正式建议不再安排此快照合入，保留目录/分支/原 README/导出状态作为只读历史。无需移植验收；档案建议的验收就是现行 main 回归/链接不退化、历史事实不改。若未来确需 Claude 原生协调能力，应基于 main 重新定义能力/协议/真实 native 验收，不把本建议扩大成当前有新移植计划。

## 8. 可复核清单与源码引用

下列链接均固定到 S：

| 快照 | manifest 的 `original_source_label` | 导出 manifest / 包装说明 / 原 README | 代码清单口径 |
|---|---|---|---|
| tracking | `ato-tracking-rectification-20261119` | [SOURCE-SNAPSHOT.json][T-manifest] / [README][T-readme] / [README.original][T-original] | 170 个映射源项、39 adapted；49 个 docs Markdown、46 个测试文件、两个根 MJS |
| efficiency | `ato-efficiency-requalification-20261120` | [SOURCE-SNAPSHOT.json][E-manifest] / [README][E-readme] / [README.original][E-original] | 202 个映射源项、52 adapted；61 个 docs Markdown、55 个测试文件、两个根 MJS |
| AGY/acpx | `ato-agy-acpx-integration-20261119` | [SOURCE-SNAPSHOT.json][A-manifest] / [README][A-readme] / [README.original][A-original] | 166 个映射源项、32 adapted；42 个 docs Markdown、44 个测试文件、四个根 MJS，另两测试 MJS |
| legacy | `codex-task-runner` | [SOURCE-SNAPSHOT.json][L-manifest] / [README][L-readme] / [README.original][L-original] | 57 个映射源项、3 adapted；12 个 docs Markdown、19 个测试文件、一个根 MJS |

“测试文件数”不等于用例数；历史文档的 591/677/936/955 等数字属于各自树/阶段。全部路径以 manifest 的真实映射为准。为便于审查下面列出四树**所有根 Python 模块**；重复模块是旧完整 runtime 的一部分，不代表重复提供新的能力。

### tracking-rectification：全部根模块

`acp_bridge.py`, `acp_outcome.py`, `agent_native_events.py`, `agent_registry.py`, `agents.py`, `approved_delivery.py`, `appserver.py`, `ato_config.py`, `budget.py`, `codex_legacy.py`, `codex_session_adapter.py`, `completion_delivery.py`, `completion_notifications.py`, `control.py`, `delegation.py`, `failure_attention.py`, `host_recheck.py`, `hub_queue_gate.py`, `interaction.py`, `internal_coordination.py`, `local_session_adapter.py`, `message_bus.py`, `notifications.py`, `permissions.py`, `pi_acp_exec.py`, `pi_acp_worker.py`, `pi_coordinator_exec.py`, `pi_session_coordinator.py`, `pi_session_profile.py`, `progress.py`, `recovery.py`, `responsibility_process.py`, `runner.py`, `session_router.py`, `task_responsibility.py`, `task_trace.py`, `transport.py`, `workflow.py`, `workflow_closure.py`, `workflow_lineage.py`, `workspace_association.py`。

根 MJS：`pi-coordinator-guard.mjs`, `pi-path-guard.mjs`。

### execution-efficiency：全部根模块

`acp_bridge.py`, `acp_outcome.py`, `agent_native_events.py`, `agent_registry.py`, `agents.py`, `appserver.py`, `ato_config.py`, `budget.py`, `capacity.py`, `codex_legacy.py`, `codex_session_adapter.py`, `codex_worker_session.py`, `control.py`, `delegation.py`, `execution_accounting.py`, `execution_metrics.py`, `failure_attention.py`, `host_recheck.py`, `hub_queue_gate.py`, `incremental_checkpoint.py`, `interaction.py`, `local_session_adapter.py`, `message_bus.py`, `notifications.py`, `permissions.py`, `pi_acp_exec.py`, `pi_acp_worker.py`, `pi_coordinator_exec.py`, `pi_session_coordinator.py`, `pi_session_profile.py`, `profile_evidence.py`, `progress.py`, `recovery.py`, `runner.py`, `runtime_selection.py`, `session_router.py`, `task_presentation.py`, `task_responsibility.py`, `task_trace.py`, `transport.py`, `verification_cache.py`, `worker_sessions.py`, `workflow.py`, `workflow_closure.py`, `workflow_lineage.py`。

根 MJS：`pi-coordinator-guard.mjs`, `pi-path-guard.mjs`。

### agy-acpx-integration：全部根模块

`acp_bridge.py`, `acp_outcome.py`, `acpx_profile.py`, `acpx_worker.py`, `agent_native_events.py`, `agent_registry.py`, `agents.py`, `appserver.py`, `ato_config.py`, `budget.py`, `codex_legacy.py`, `codex_session_adapter.py`, `control.py`, `delegation.py`, `external_coordinator.py`, `external_operator.py`, `failure_attention.py`, `host_recheck.py`, `hub_queue_gate.py`, `interaction.py`, `local_session_adapter.py`, `message_bus.py`, `notifications.py`, `permissions.py`, `pi_acp_exec.py`, `pi_acp_worker.py`, `pi_coordinator_exec.py`, `pi_session_coordinator.py`, `pi_session_profile.py`, `progress.py`, `recovery.py`, `runner.py`, `session_router.py`, `task_trace.py`, `transport.py`, `workflow.py`, `workflow_closure.py`, `workflow_lineage.py`。

根 MJS：`acpx_bridge.mjs`, `acpx_native_proxy.mjs`, `pi-coordinator-guard.mjs`, `pi-path-guard.mjs`。

### legacy-managed-mailbox：全部根模块

`agents.py`, `appserver.py`, `budget.py`, `control.py`, `delegation.py`, `interaction.py`, `mailbox_routes.py`, `message_adapters.py`, `message_bus.py`, `permissions.py`, `progress.py`, `recovery.py`, `runner.py`, `transport.py`, `workflow.py`。

根 MJS：`pi-path-guard.mjs`。

## 9. 本轮验收与停点

只在独立 worktree 运行当前 main 的既有测试，不运行四份快照的测试或历史 packager/probe。全量入口是：

```sh
.venv/bin/python scripts/run_tests.py
.venv/bin/python scripts/check_architecture.py
```

本轮实测结果（2026-11-22）：

| 门禁 | 结果 |
|---|---|
| 全量 `scripts/run_tests.py` | **800 tests，163.503 秒，0 failures / 0 errors / 0 skips** |
| `scripts/check_architecture.py` | 根/运行图 **8 / 8 对**，均为历史 31 对基线子集 |
| 源码/测试与八核心 | 相比 `0f45125`，全部运行代码和测试字节不变，八核心 AST 亦不变；只有三份 Markdown 变更 |
| 文档链接 | 57 份 Markdown 的 **183 个相对链接，0 失效**；43 个 S 固定源码引用的本地 Git 对象目标与行号均存在（不等于在线 HTTP 核验） |
| 快照清单 | **595 个 published source SHA-256** 逐项匹配；四份 manifest/历史导出状态没有改动 |

测试/运行代码/依赖基线文件不改，只变更本文、文档索引及 development-status 的审计指针。本机仓库外构建日志目录（未入库）保留了本次离线验收回执与源码比较摘要，其中不包含私有原始任务/历史/认证，不作为运行代码或历史资格导入。

提交后停止，待用户选择每个快照的具体后续动作。800 项 main 回归只证明本次文档没有破坏现行基线，**不使四份快照获得历史宿主/真实记录/native benchmark/模型或上线资格**。本轮不合 main、不 push、不删除/force-push 历史分支，不触碰安装入口及线上状态。

[T-manifest]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/tracking-rectification/SOURCE-SNAPSHOT.json#L1
[T-readme]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/tracking-rectification/README.md#L1
[T-original]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/tracking-rectification/README.original.md#L1
[E-manifest]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/execution-efficiency/SOURCE-SNAPSHOT.json#L1
[E-readme]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/execution-efficiency/README.md#L1
[E-original]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/execution-efficiency/README.original.md#L1
[A-manifest]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/agy-acpx-integration/SOURCE-SNAPSHOT.json#L1
[A-readme]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/agy-acpx-integration/README.md#L1
[A-original]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/agy-acpx-integration/README.original.md#L1
[L-manifest]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/legacy-managed-mailbox/SOURCE-SNAPSHOT.json#L1
[L-readme]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/legacy-managed-mailbox/README.md#L1
[L-original]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/legacy-managed-mailbox/README.original.md#L1
[T-task]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/tracking-rectification/docs/tracking-rectification/task.md#L1
[T-workspace]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/tracking-rectification/workspace_association.py#L50
[T-approved]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/tracking-rectification/approved_delivery.py#L207
[T-internal]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/tracking-rectification/internal_coordination.py#L180
[T-responsibility]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/tracking-rectification/task_responsibility.py#L163
[T-completion]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/tracking-rectification/completion_delivery.py#L177
[T-host]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/tracking-rectification/docs/tracking-rectification/operator-handoff.md#L1
[T-probe]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/tracking-rectification/scripts/verify_tracking_readonly.py#L310
[E-task]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/execution-efficiency/docs/execution-efficiency/task.md#L1
[E-runtime]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/execution-efficiency/runtime_selection.py#L175
[E-accounting]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/execution-efficiency/execution_accounting.py#L21
[E-worker]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/execution-efficiency/worker_sessions.py#L171
[E-checkpoint]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/execution-efficiency/incremental_checkpoint.py#L291
[E-cache]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/execution-efficiency/verification_cache.py#L350
[E-metrics]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/execution-efficiency/execution_metrics.py#L154
[E-remediation]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/execution-efficiency/docs/execution-efficiency/continuation-regression-remediation.md#L1
[E-validation]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/execution-efficiency/docs/execution-efficiency/validation.md#L1
[E-drift]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/execution-efficiency/docs/execution-efficiency/continuation-profile-drift.md#L1
[A-task]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/agy-acpx-integration/docs/external-coordinator/task.md#L1
[A-external]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/agy-acpx-integration/external_coordinator.py#L56
[A-profile]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/agy-acpx-integration/acpx_profile.py#L196
[A-bridge]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/agy-acpx-integration/acpx_bridge.mjs#L10
[A-proxy]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/agy-acpx-integration/acpx_native_proxy.mjs#L10
[A-validation]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/agy-acpx-integration/docs/external-coordinator/validation.md#L1
[A-demo]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/agy-acpx-integration/operator-examples/demo-README.md#L1
[A-host]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/agy-acpx-integration/docs/external-coordinator/origin-delivery-handoff.md#L1
[L-validation]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/legacy-managed-mailbox/docs/message-delivery-validation.md#L1
[L-routes]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/legacy-managed-mailbox/mailbox_routes.py#L74
[L-adapters]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/legacy-managed-mailbox/message_adapters.py#L80
[L-upgrade]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/legacy-managed-mailbox/scripts/upgrade_runtime.py#L71
[L-core]: https://github.com/Aias00/agent-task-orchestrator/blob/7ec8221c08c55cdee85f5b9767e66fcd53a47876/snapshots/legacy-managed-mailbox/delegation.py#L10
