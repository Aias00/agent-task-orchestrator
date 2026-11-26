# H1 离线实现与验收边界

状态：**已评审合入 `main`（离线候选，未安装激活运行态）**。遵循
[批准设计](hub-orchestrator-design.md) 与 [CLI Spike](h1-cli-spike.md)。
离线实现已通过第 8 节 O1–O10 门禁；默认不继承本机 Codex 认证与环境代理，仅在操作者显式传入 `--worker-codex-home` 或 `--proxy`（仅允许无凭据回环地址如 `http://127.0.0.1:7890`）时注入对应配置（`ATO_HOME` 仍保持隔离）。

## 实现与使用入口

以下入口与安装命令适用于原 H1 候选源码。当前仓库通过统一的
[`ato` CLI](../README.md#quick-start) 提供公开入口，H1 解析器保存在
[离线测试 fixture](../tests/fixtures/hub_cli.py) 中。

10 个根模块形成单向依赖图，八核心与原有 55 命令 CLI 保持不变。
`hub_cli.py` 是独立 argparse 入口；可从选定源码 checkout 使用，也可显式安装 `ato-hub`。
本次日常可用性补丁是独立候选，尚待评审，不表示安装或运行态已经更新。

安装仅在获得相应授权后执行，`BIN_DIR` 和 `HUB_SKILLS_DIR` 由操作者选定：

```sh
.venv/bin/python scripts/install.py --bin-dir "$BIN_DIR" --hub --hub-skills-dir "$HUB_SKILLS_DIR"
```

原有 `ctask` 默认安装及 `--helpers` 保持不变；`--hub` 额外生成 `ato-hub`，
`--hub-skills-dir` 独立选择 Skill 安装位置。已有目标默认拒绝覆盖，审查后才显式使用 `--force`。
安装的 Skill 指向该源码版本的说明文档。`ato-hub` 在未设置 `ATO_HOME` 时默认使用
`$HOME/.local/share/ato-hub-state`，与现有 ctask 状态隔离；已设置的 `ATO_HOME` 则沿用。
未安装时，使用 `.venv/bin/python hub_cli.py` 并显式设置独立 `ATO_HOME`。

日常先发现已有长期 Hub，避免每个任务重复创建：

```sh
export ATO_HOME="$PWD/../h1-private-state"
ato-hub hub-list --repo "$PWD"
# 无匹配 Hub 时才创建；已有时复用返回的 hub_id。
ato-hub hub-create --repo "$PWD" --kind agy
ato-hub task-create HUB_ID --spec "$PWD/hub-task-spec.json"
# task-run 会调用 Worker 与当前 Hub 审阅器，仅在具备相应授权时执行。
ato-hub task-run HUB_ID TASK_ID
ato-hub task-status HUB_ID TASK_ID
ato-hub task-stop HUB_ID TASK_ID
```

`hub-list` 只读输出 hub_id、repo、active_agent、hub_busy、active_session_state、task_count，
按 ID 排序；`--repo` 要求真实 canonical 仓库路径并精确过滤。不存在账本时返回空列表，
不创建目录或锁文件，不输出会话 ID、凭据、任务 prompt 或 transcript。

任务规格见设计 4.1.1；创建时复用现行 `workflow.validate` / `runner.validate`
冻结默认值并关闭 memory，再封存 `spec_hash`。仓库、规格文件、父目录、账本路径必须为
canonical 绝对路径，拒绝软链别名；路径来自操作者输入，不硬编码本机目录。
主控由 hub `active_agent` 决定，`review_policy.agent` 保留原规格事实；切换只影响后续 turn。

账本位于显式 `$ATO_HOME/hubs/HUB_ID/`，不置于目标仓库。`hub-status`、`task-status`
展示 busy；`hub-attach-cmd` 只返回命令，锁持有时拒绝；`hub-switch-agent`、`hub-rollover`
要求 `--source`。新工作树在仓库外，已有分支/目录绝不覆盖。
`worktree_policy.shared_env_links` 仅允许 `.venv`、`venv`、`node_modules`、
`integrations/pi-acp/node_modules`，例如 `[".venv", "node_modules"]`。
主仓源目录须实际存在、非软链、未被 Git 追踪且被忽略，目标已存在则拒绝。
共享依赖只显式供给软链，不授予写权限，审阅 profile 禁止写入目标。
完整性采样不遍历共享依赖；Git 追踪的同名目录仍作为源码采样。
清理先核验原软链目标，保留主仓依赖，只移除供给的软链。

Worker 沿用未改动 Workflow/Runner，使用私有 `worker-ato` 与 `worker-home`，
不继承当前原会话、actor、loader、proxy 或密钥环境。默认私有 native home 不继承本机认证；
真实凭据供给、运行资格与业务授权另行评审。获准实网运行时，明确选择 canonical native home
与无凭据回环代理，不依赖继承环境中的登录或代理：

```sh
ato-hub task-run HUB_ID TASK_ID --worker-codex-home "$WORKER_CODEX_HOME" --proxy http://127.0.0.1:7890
```

`--worker-codex-home` 仅用于 Worker，不能与 Hub 账本重叠；`ATO_HOME` 仍隔离。
`--proxy` 也可用于 `task-review-only`，拒绝远端、userinfo/密码、路径或 query。
本轮测试不读取真实认证、不启动代理、不调用模型。测试从可信构造器注入合成 Worker/Reviewer，
未将可执行路径放进模型可改规格。宿主 checks 在清洁环境运行，依赖使用明确 argv 或共享依赖。

发布工具保持原 `ENTRIES` 三入口常量。仅在安装目录已有 `ato-hub` 或显式 `--include-hub`
时，dry-run/activate 纳入第四入口，要求快照包含 `hub_cli.py`，由安装器 `--helpers --hub`
生成入口。事务保存每个入口原字节、权限及此前不存在的事实；回滚恢复旧 Hub，或删除本次新增的
Hub 入口。原三入口混合收敛、observer、策略、mailbox 与任务状态边界保持不变。

## 闭环与硬停点

首次 task-run 先预置并执行冻结的首个 stage，现有回归检查全绿也不会略过 Worker。
未提供 stage 时，以冻结 objective 和原 command 检查生成一次 hub-initial 实现任务；
既无预置阶段也无 command 检查时，以 INITIAL_WORK_CHECKS_REQUIRED 拒绝启动。
其余阶段继续由未改动 Workflow 按未通过 criterion 选择，不改原检查、授权或预算。
只需检查/审阅已有代码时使用 task-review-only，不通过 task-run 隐式跳过工作。
命令检查全过、仅 manual 待确认且无活动
owner/process/child 时才审阅。返修使用同一 worktree，新建 workflow 保留累计历史，
逐项复制原 acceptance、授权、sandbox、permissions、agent 和预算边界，只有 blocker
文本进入修复 prompt。原检查已通过时，外层预置修复阶段，避免 Workflow 因全绿而跳过修复。

审阅前后均执行 `hub_worktree.snapshot_integrity`；源码、ignored 临时文件、索引、HEAD
或共享依赖入口改变即 `REVIEWER_MUTATION_VIOLATION`。blocker 指纹使用当前源码内容摘要，
包括未提交修复；ignored 任务回执只进入完整性绊线，不污染源码指纹。

`approve` → `ready_for_user_review` 是硬终态，再次 task-run 不启动进程。
`request_changes` 仅 blocker 可返修，重复指纹/轮次耗尽阻塞；失败检查不调用 Reviewer。
`task-review-only` 只核查已绑定 worktree 的原 checks，再审阅一次，不启动 Worker、
planner 或返修；不能越过旧 STOP、未知 owner/delivery 或轮次上限。中断中的
running_worker/reviewing/fixing 再执行只标记阻塞，不认领旧 PID 或重放。

仅复核的宿主检查也须在仓库锁内取得既有 execution lease；有效 origin/session-host
租约、或过期但原生活动未知的租约均拒绝执行。检查期间续租，退出只释放本次所有权，
不能以仓库锁空闲代替持久执行归属核验。
检查沿用该 workflow 的既有预算和累计 elapsed_seconds：enforced 模式在剩余总额度
耗尽时停为 limit，advisory 模式只记账和显示超估；单条 check_seconds 与 Hub STOP
继续有效。成功、失败、超时及停止的清理耗时均计入，复核不会清零历史或确认 manual。

Hub STOP 不等待 `.hub.lock`；外层 Workflow guard 传至子 Runner，复用有界组清理。
Reviewer 使用独占组、出生身份、≤100ms 轮询与 TERM/KILL；未知/回收身份报 cleanup_blocked。
自动循环保留工作树；仅 `worktree-cleanup --confirm` 尝试清理，脏树/锁拒绝且保留分支。
绝无自动 acknowledge、actor grant、merge、push 或激活。

通知默认 `notice=recorded_only`；可信宿主可注入回调。调用前持久化
`delivery_unknown`，未知投递不重试，不把未配置通知声称为已唤醒交互窗口。

## 三家协议与资格

| 适配器 | 已落实的 Spike 协议 | 保留边界 |
| --- | --- | --- |
| AGY | 外层只读 sandbox-exec，SUCCESS/conversation_id/structured_output 严格校验，resume 精确 ID | 无 plan/disable 冲突，无内层 sandbox；持久会话只放行原生 antigravity-cli 目录 |
| Codex | 原生 read-only Seatbelt；resume 仅 cwd + 配置；thread.started + `-o` JSON | 无双层 Seatbelt，每轮强制事后绊线 |
| Claude | 外层只读 sandbox + safe/restricted/Read,Glob,Grep/严格 MCP/schema，auth_blocked 分类 | session_resume / structured_output / interactive_attach 仍 unknown；resume_if_supported 回退 fresh_only + 摘要，require_resume 拒绝 |

离线测试是合成 CLI 协议验证，不构成新增真实 Provider、Claude 登录或原生续接资格。
`doctor` 只做版本探针，不证明认证/模型可用。

## O1–O10 验收映射

| ID | 离线证据 |
| --- | --- |
| O1 | `test_hub_foundations.py`：严格 JSON、裁决互斥、flag/路径拒绝、确定性指纹 |
| O2 | `test_hub_adapters.py`：三厂商首/续轮、协议坏包、鉴权/超时/非零、attach/doctor |
| O3 | foundations：真实 macOS 子进程写拒绝 worktree/.git/主仓/trusted；adapters/loop：篡改即阻塞 |
| O4 | foundations：固定基线、碰撞、依赖软链范围、脏树/持锁清理拒绝 |
| O5 | loop：真实 Workflow + 合成 Worker + 真实宿主检查 + blocker 修复 + approve，仅停人工终审 |
| O6 | loop/sandbox：轮次上限、重复 blocker、失败不审阅、鉴权/blocked/篡改、真实 TERM-ignore 组 STOP |
| O7 | foundations/loop：agy→claude→codex、旧 ID 保留/隔离、摘要、自动/显式 rollover |
| O8 | adapters/loop：sentinel、清洁环境、公开账本/报告扫描、私有 0600 stderr 有界 |
| O9 | foundations/loop：原 checks/权限不变、额外授权字段拒绝、软链逃逸拒绝、无自动人工确认 |
| O10 | 三次全量回归、py_compile、旧 8 对依赖子集、八核心 AST/CLI fixture 零漂移、hub_adapter_ 禁入、分发/索引 |

复验命令（合成隔离环境，无真实模型）：

```sh
.venv/bin/python scripts/run_tests.py
.venv/bin/python scripts/check_architecture.py
```

各阶段日志、编译和固定字节检查收据位于忽略目录 `build/h1-offline/`。
全量编译将源码 Python 文件写入临时 pyc 目录，避免写入源码树。
最终计数和三次提交见本轮验收报告；历史设计/Spike 的时点事实保持原样。
