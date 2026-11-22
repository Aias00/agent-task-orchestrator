# H1：可换长期主控（AGY / Claude / Codex）与隔离执行闭环设计

状态：**设计已获用户批准（2026-11-22），前置只读 CLI Spike（S1–S4）已通过（见 [h1-cli-spike.md](h1-cli-spike.md)），准予推进离线实现（H1-A / H1-B / H1-C）**。设计基线为 `main@e162395`（包含 Batch 1–3、P3-1、W1、W2-1 离线实现与 W3 审计定性）。

本轮仅新增本设计文档并登记索引；**不修改任何运行代码、测试或依赖文件，不操作本机运行态或安装入口，不调用真实云端模型**。后续的前置 CLI 实测 Spike、离线实现与真实端到端试跑均需分步评审和单独授权。

---

## 1. 背景、目标与非目标

### 1.1 问题与目标
在 ATO 自身的 Batch 1–3、P3-1、W1、W2-1 与 W3 迭代中，已经验证出一套高可靠研发闭环：

`主控规划定约束 → 执行器在独立 Git worktree 实现并跑宿主验收 → 主控独立只读审阅 → 有界返修复审 → 人类终审拍板合入`

当前痛点在于：指令派发、进度盯盘、审阅触发、返修回传与隔离 worktree 创建全部靠人工复制粘贴，且手动多开窗口极易出现并行分支撞车（如 W2-1 曾出现三分支并跑）。

**H1 的核心目标**是将这一闭环产品化为 ATO 的一等能力，同时满足 4 项关键要求：
1. **主控可换（Vendor-Neutral Hub）**：主控/审阅角色通过统一 `HubAdapter` 契约解耦，首批支持 **AGY**，同契约原生支持 **Claude Code** 与 **Codex**，并保留未来挂接 `acpx` ACP 长会话后端的同接口槽位；执行角色（默认 Codex，亦可切 Claude/Pi）与主控角色独立配置、支持异构配对。
2. **长期主控体验（逻辑常驻 + 账本交接）**：不依赖易崩/易失联的永驻 LLM 大进程，而是采用**确定性薄调度器 + 原生会话恢复（`resume_turn`）+ 权威磁盘账本（`handoff-brief.md`）**：
   - 同厂商跨轮次靠原生 `session_id` 保持上下文连续，并随时给出原生 `attach` 命令供人类切入交互终端接管；
   - 跨厂商中途切换主控（如 AGY → Claude → Codex）或会话达到轮次上限滚动（Rollover）时，从 ATO 磁盘账本显式重建上下文，绝不伪造跨厂商原生续接。
3. **隔离执行与有界审阅闭环**：每个任务自动隔离在专属 Git worktree 与新分支，复用未改动的 ATO `workflow.py` / `runner.py` 驱动执行器与宿主独立 `checks`；全部自动验收通过后自动进入只读审阅，仅对 `blocker` 触发有界 `Review → Fix` 返修循环，最终停在 `ready_for_user_review` 等人类终审。
4. **吸收 W3 审计结论**：结合 [W3 审计](w3-candidate-snapshots-audit.md) 对 `agy-acpx-integration` 的 `REDESIGN_FROM_MAIN` 定性，吸收其「精确范围约束、禁模型越权、失败不静默放行」原则，但**不绑死 `acpx_codex` 重量级依赖链**，也不回退到旧循环依赖图。

### 1.2 明确非目标（H1 不做什么）
- **零改动八核心模块**：不修改 `control`、`recovery`、`interaction`、`delegation`、`workflow`、`session_router`、`message_bus`、`transport`，保持 [w2-core-ast.json](../tests/fixtures/w2-core-ast.json) 100% 不变、零 vendor import。
- **零改动现有 55 个 `ctask` CLI 命令面**：保持 [cli-surface.json](../tests/fixtures/cli-surface.json) 与 [test_cli_surface.py](../tests/test_cli_surface.py) 100% 不变；H1 通过独立入口模块 `hub_cli.py` 提供控制面，未单独批准发布/安装前不写入 `~/.local/bin`。
- **不改动 `acpx` 仓库**：不把 `acpx` 强塞进 H1 最小闭环；仅在 `HubAdapter` 契约层保留未来可插拔槽位。
- **不替人类做最终决定**：技术审阅 `approve` **绝不**自动触发 `acknowledge`（人工验收）、合入 `main`、`git push`、删除分支或切换本机运行态。
- **不把离线全绿冒充真实 CLI 资格**：离线单测全程使用合成假 CLI fixture，与单独授权的最小真实 CLI Spike 严格分离。

---

## 2. 基线源码与本机 CLI 事实核查（离线只读）

### 2.1 ATO 现行源码基线约束（`main@e162395`）

| 基线模块 / 门禁 | 源码事实 | 对 H1 设计的硬约束 |
| --- | --- | --- |
| [workflow.py](../workflow.py) L19–88、L207–228、L362–469 | 已有不可变合约、`contract_hash`、宿主独立 `checks`、仓库锁 `.codex/tasks/.worker.lock`、`manual` 人工验收停点（`needs_input`） | H1 外层编排器直接组合 `workflow.create` 与 `Workflow.execute`，不修改 `workflow.py` AST |
| [runner.py](../runner.py) L105–189 | 已有 `STOP` 守卫、W1 有界退出感知 `Popen.wait`（≤150ms）、进程组 `SIGTERM → 2s → SIGKILL` 清理与 `cleanup_blocked` | `hub_sandbox.py` 沿用同等进程组、退出感知轮询和清理语义 |
| [agents.py](../agents.py) L242–355 | 已有 macOS `sandbox-exec` 写限制、敏感路径 7 项排除、Claude `--safe-mode --restricted --tools Read,Glob,Grep --json-schema` 与鉴权阻塞识别 | `hub_adapter_claude.py` 与 `hub_sandbox.py` 对齐其只读工具与鉴权失败分类，不破坏现有 worker 逻辑 |
| [pi_toolchain.py](../pi_toolchain.py) | 纯 stdlib 叶模块，提供 `write_exclusions(runtime)` 保护运行时根与可信目录 | `hub_sandbox.py` 可单向复用 `write_exclusions`，不引入反向依赖 |
| [check_architecture.py](../scripts/check_architecture.py) L11–54、[结构测试](../tests/test_architecture_structure.py) L37–46 | 自动扫描根 `*.py` 与 `ato_cli/`（含函数内延迟 import），要求双向依赖 ≤8 且为 [31 对历史基线](../tests/fixtures/architecture-baseline.json) 子集 | H1 新模块置于根 `hub_*.py`，严格单向依赖，**新增 0 对双向依赖** |
| [test_pi_production_profile.py](../tests/test_pi_production_profile.py) L153–166 | 校验八核心 AST SHA-256 等于 [w2-core-ast.json](../tests/fixtures/w2-core-ast.json)，并禁止八核心导入 vendor 前缀 | H1 保持八核心零变更，并扩充 vendor AST 禁入前缀覆盖 `hub_adapter_` |
| [cli-surface.json](../tests/fixtures/cli-surface.json) | 冻结现有 55 个 `ctask` 子命令/别名/参数 | H1 新增独立模块 `hub_cli.py`，不改 `ato_cli/parser.py` |
| [pyproject.toml](../pyproject.toml) & 元数据测试 | `py-modules` 必须精确等于根 `*.py` 集合；`docs/README.md` 必须覆盖所有 `docs/**/*.md` | 所有新 `hub_*.py` 必须登记进 `pyproject.toml`、分发清单与 [源码图](source-map.md) |

### 2.2 本机三家 CLI 能力离线核查（零模型调用）

通过离线读取 `--version` 与 `--help` 核验当前本机已安装版本与真实参数面（不含任何网络/模型请求）：

| Agent | 实测版本 | 非交互单轮 & 结构化输出 | 按 ID 续接会话 | 人类交互接管命令 | 关键参数边界（实测发现） |
| --- | --- | --- | --- | --- | --- |
| **AGY** | `1.5.14` | `agy -p --mode plan --output-format json --json-schema <file> --disable-slash-commands --print-timeout <t> [--model <m>] [--effort <e>]` | `--conversation <id>` | `agy --conversation <id>` | 自带 `--sandbox`；**需在 Spike 实测 `--sandbox` 与外层 macOS `sandbox-exec` 是否存在 Seatbelt 嵌套冲突**，以及 JSON envelope 确切字段名 |
| **Claude Code** | `2.1.325` | `claude -p --safe-mode --restricted --disable-slash-commands --output-format stream-json --verbose --permission-mode dontAsk --tools Read,Glob,Grep --allowedTools Read,Glob,Grep --strict-mcp-config --mcp-config '{"mcpServers":{}}' --json-schema <json> [--model <m>] [--effort <e>]` | 首轮 `--session-id <uuid>`；续轮 `--resume <id>`（去掉 `--no-session-persistence`） | `claude --resume <id>` | `agents.py` 已验证其无状态只读参数；开启持久会话恢复后需允许写其原生会话元数据目录（见 4.2） |
| **Codex** | `0.171.0` | `codex exec --sandbox read-only -c approval_policy="never" --json --output-schema <file> -o <out> [--model <m>] -` | `codex exec resume <id> -c sandbox_mode="read-only" -c approval_policy="never" --json --output-schema <file> -o <out> [--model <m>] -` | `codex resume <id>` | **关键发现**：`codex exec resume --help` **没有** `-s/--sandbox` 和 `-C/--cd` 参数，只能靠 `cwd=<worktree>` + `-c sandbox_mode="read-only"` + 外层只读沙箱 + 事后树校验兜底 |

> 诚实声明：以上 CLI `--help` 仅证明参数语法存在，**不证明真实运行时输出格式、嵌套沙箱兼容性或鉴权状态**；所有 `HubCapabilities` 在通过第 7 节 Spike 实测前不得虚标为 `supported`。

---

## 3. 总体架构与模块边界

### 3.1 分层拓扑

```text
用户（唯一最终权威：定目标 / 评审设计 / acknowledge / merge / push / 切换运行态）
   │  随时按 attach_argv 进入原生交互终端，或操作 hub_cli.py
   ▼
hub_cli.py ──► hub_loop.py（外层有界编排状态机，严格单向依赖）
                  ├──► hub_ledger.py（权威磁盘账本、长期会话指针、跨厂商交接摘要、滚动策略）
                  ├──► hub_worktree.py（隔离 Git worktree 供给、diff 证据包、事后篡改绊线）
                  ├──► hub_registry.py（代码拥有的主控适配器注册表）
                  │       ├──► hub_adapter_agy.py    ──┐
                  │       ├──► hub_adapter_claude.py ──┼──► hub_contract.py（纯 stdlib）
                  │       ├──► hub_adapter_codex.py  ──┤     hub_sandbox.py（只读 OS 边界 + 进程组）
                  │       └──► [预留] hub_adapter_acpx.py（H1 之外，同契约按需接入 acpx）
                  └──► workflow.py / runner.py（现有未改动 ATO 内核：执行 Codex + 宿主独立 checks）
```

### 3.2 模块职责与依赖方向表

所有新模块均位于仓库根目录（`hub_*.py`），自动纳入 [check_architecture.py](../scripts/check_architecture.py) 静态图扫描，严格无环：

| 新模块 | 职责 | 允许导入的内部模块 | 禁止事项 |
| --- | --- | --- | --- |
| `hub_contract.py` | 版本化契约数据结构（`HubCapabilities`、`HubAgentSpec`、`HubTurnRequest`、`HubTurnResult`、`HubAdapter` 协议）、`REVIEW_VERDICT_SCHEMA`、`HUB_TASK_SPEC_SCHEMA`、严苛 JSON 校验与 `blocker_fingerprint` | **仅标准库（0 内部导入）** | 不含任何 subprocess、文件 I/O 或厂商分支 |
| `hub_sandbox.py` | macOS 只读 `sandbox-exec` profile 组装、最小清洁环境、独占进程组运行（≤150ms 退出感知轮询与 STOP/硬超时）、私有 0600 `stderr.log` 与脱敏错误码 | `pi_toolchain`（仅复用 `write_exclusions`） | 不回调适配器、不记录凭据或原始敏感 stderr 到公开回执 |
| `hub_adapter_agy.py` | AGY CLI 只读主控/审阅适配器（`kind="agy"`） | `hub_contract`、`hub_sandbox` | 禁止读取任务指定可执行路径；禁止触碰八核心 |
| `hub_adapter_claude.py` | Claude Code CLI 只读主控/审阅适配器（`kind="claude"`） | `hub_contract`、`hub_sandbox` | 同上 |
| `hub_adapter_codex.py` | Codex CLI 只读主控/审阅适配器（`kind="codex"`） | `hub_contract`、`hub_sandbox` | 同上 |
| `hub_registry.py` | 代码拥有注册表（`register` / `get` / `kinds` / `doctor_all`） | `hub_contract` + 三个 `hub_adapter_*` | 禁止动态 import、模块路径配置或隐式探测启用 |
| `hub_worktree.py` | 隔离 Git worktree 创建、可选被忽略开发依赖软链（如 `.venv`）、`snapshot_integrity` 篡改检测、`diff_bundle` 组装、显式人工清理 | `ato_core` | 禁止在 `main` 检出目录直接写代码；禁止自动删除分支/worktree |
| `hub_ledger.py` | `$ATO_HOME/hubs/<hub-id>/` 私有账本（`0700`/`0600` + `fcntl.flock`）、长期会话记录、`switch_agent` 跨厂商切换、`rollover` 会话滚动、`handoff-brief.md` 生成 | `ato_core`、`ato_config`、`hub_contract` | 禁止将账本存进目标仓库 Git 树；禁止跨厂商复用原生 `session_id` |
| `hub_loop.py` | 有界 `Work → Host Checks → Read-Only Review → Bounded Fix` 编排主循环 | `ato_core`、`hub_contract`、`hub_registry`、`hub_worktree`、`hub_ledger`、`workflow`、`runner` | 严禁 `workflow`/`runner` 反向导入任何 `hub_*`；严禁自动 `acknowledge`/merge/push |
| `hub_cli.py` | 独立 CLI 入口（`doctor`、`hub-*`、`task-*`、`worktree-cleanup`） | `ato_core`、`hub_contract`、`hub_registry`、`hub_worktree`、`hub_ledger`、`hub_loop` | 不修改 `ato_cli/parser.py` 现有 55 命令面 |

**依赖门禁验证**：上述 10 个模块构成纯 DAG，**新增双向依赖对数为 0**（全局维持 8 / 8 且属于历史 31 对基线子集），八核心模块零改动。

---

## 4. 核心设计细节

### 4.1 角色分离与主控可换契约（`HubAdapter`）

#### 4.1.1 主控配置与执行配置彻底解耦
一个 Hub 任务规格（`hub-task-spec.json`，`schema_version = 1`）将执行端与主控/审阅端显式分离：

```json
{
  "schema_version": 1,
  "task_id": "feat-example-20261122",
  "repo": "/abs/canonical/repo/path",
  "base_ref": "main",
  "branch": "feat/example-20261122",
  "worker_contract": {
    "objective": "...",
    "scope": "...",
    "authorization": "...",
    "agent": {"kind": "codex"},
    "sandbox": "workspace-write",
    "acceptance": [
      {"id": "tests", "description": "Full offline suite", "kind": "command", "checks": [[".venv/bin/python", "scripts/run_tests.py"]]},
      {"id": "user_signoff", "description": "User reviews final diff and approves merge", "kind": "manual"}
    ],
    "stages": [...]
  },
  "review_policy": {
    "enabled": true,
    "agent": {"kind": "agy", "model": null, "effort": "high"},
    "session_mode": "resume_if_supported",
    "max_review_rounds": 2,
    "timeout_seconds": 300,
    "checklist": [
      "No edits to 8 protected core modules",
      "No secrets or local absolute paths committed"
    ],
    "design_docs": ["docs/hub-orchestrator-design.md"]
  },
  "worktree_policy": {
    "parent_dir": null,
    "shared_env_links": [".venv", "integrations/pi-acp/node_modules"]
  }
}
```

- **合法组合示例**：`worker=codex` + `review=agy`（当前主力）；`worker=codex` + `review=claude`；`worker=claude` + `review=codex`；任意时刻可通过 `hub_cli.py hub-switch-agent` 切换 Hub 默认主控。
- **强制 `manual` 门禁**：当 `review_policy.enabled = true` 时，校验器强制要求 `worker_contract.acceptance` 至少包含一项 `kind = "manual"` 的最终人工验收项（防止标准 ATO `Workflow.execute()` 在未审阅前就将子 workflow 标为 `completed`）。

#### 4.1.2 `HubAdapter` 统一协议与诚实能力声明
```python
class HubAdapter(Protocol):
    kind: str
    def capabilities(self) -> HubCapabilities: ...
    def doctor(self) -> dict: ...
    def run_turn(self, request: HubTurnRequest) -> HubTurnResult: ...
    def attach_argv(self, session_id: str, workspace: Path) -> list[str]: ...
```

`HubCapabilities` 显式区分 6 项能力，每项均为 `supported` / `unsupported` / `unknown` + 代码拥有证据（缺省为 `unknown`，绝不从“本机已装 CLI”推断支持）：
1. `structured_output`：支持严格 JSON Schema 结构化裁决输出
2. `session_resume`：支持按精确 `session_id` 恢复上下文
3. `interactive_attach`：支持人类在交互终端直接接管同一 `session_id`
4. `read_only_cli`：CLI 原生具备只读/plan/受限工具模式
5. `os_sandbox_compatible`：已实测可与 macOS只读 `sandbox-exec`（或等效 OS 写拒绝边界）协同工作
6. `long_lived_stream`：是否支持持久常驻流/ACP 通道（H1 三个原生 CLI 适配器先显式标为 `unsupported`，留给未来 `acpx` 或 `stream-json` 后端）

- **Session Mode 策略**：
  - `fresh_only`：每轮审阅起全新无状态会话，上下文完全来自 ATO 证据包 + `handoff-brief.md`（隔离度最高，无需依赖原生 resume）。
  - `resume_if_supported`（默认推荐）：若当前适配器 `session_resume == supported`，按 Hub 账本记录的 `native_sessions[kind].session_id` 续接；否则显式退回 `fresh_only` 并在事件流中记录 `session_mode_effective="fresh_only"`，绝不伪造续接。
  - `require_resume`：若适配器未通过 resume 资格直接 fail-closed 拒绝。

### 4.2 长期主控生命周期：逻辑常驻、跨厂商切换与会话滚动

#### 4.2.1 私有账本布局（`$ATO_HOME/hubs/<hub-id>/`）
所有目录 `0700`、文件 `0600`、原子 `save`（沿用 `ato_core.save`），互斥锁 `.hub.lock`（`fcntl.flock(LOCK_EX | LOCK_NB)`）：
- `hub.json`：Hub 身份、`repo`（canonical 绝对路径）、`active_agent`、`native_sessions`（按 `kind` 隔离记录 `{session_id, provenance, created_at, last_used_at, turn_count, retired_sessions[]}`）、`session_rollover_policy`（默认 `max_turns = 20`）。
- `handoff-brief.md`：确定性生成的项目连续性摘要（≤32 KiB 上限，仅含非敏感事实：目标、当前分支/基线 commit、已完成阶段、最近审阅结论、未关闭 blocker、工作树路径、待人工确认项）。
- `tasks/<task-id>/`：
  - `spec.json`（冻结任务规格）与 `state.json`（含 `spec_hash`、`status`、`worktree`、`active_workflow_dir`、`review_round`、`blocker_fingerprints`、`generation`、`last_verdict`）
  - `events.jsonl`（单调追加事件流）
  - `reviews/round-N.json`（结构化审阅回执）与 `reviews/round-N.md`（人类可读审阅报告）
  - `STOP`（急停文件）

#### 4.2.2 同厂商长期记忆 vs 跨厂商换主控 vs 防上下文膨胀
1. **同厂商长期记忆**：每次 `run_turn` 成功返回原生 `session_id` 后写入 `hub.json`；下次唤醒直接传 `resume_session_id`。人类随时可运行 `hub_cli.py hub-attach-cmd <hub-id>` 获取精确命令（如 `agy --conversation <id>` / `claude --resume <id>` / `codex resume <id>`）进交互界面接管；**互斥保护**：当 `task-run` 持有 `.hub.lock` 正在运行后台主控 turn 时，状态明示 `hub_busy=true`，避免与前台交互会话争抢同一原生会话。
2. **跨厂商切换主控（`hub-switch-agent`）**：
   - 将旧 `kind` 的原生会话标记为 `parked`（保留审计，绝不传给新厂商）；
   - 记录 `agent_switched` 事件；
   - 新厂商首轮强制以 `provenance = "ledger_reconstruction_from_<old_kind>"` 启动全新原生会话，并在 prompt 头部注入权威 `handoff-brief.md` + `spec.json` + 最新 `reviews/round-N.json`。
3. **会话滚动（`Rollover`）**：当同一原生会话累计 `turn_count >= max_turns`（默认 20）或人工显式执行 `hub-rollover` 时，将当前 `session_id` 移入 `retired_sessions`，下一轮基于 `handoff-brief.md` 开启新会话，防止长会话上下文污染和退化。

### 4.3 隔离 Git Worktree 生命周期（`hub_worktree.py`）

汲取 W2-1 三分支撞车教训，H1 将 worktree 隔离做成结构性强制：
1. **强制隔离供给（`provision`）**：
   - 校验 `repo` 是合法 Git 仓库且 `base_ref` 可解析为确定 commit SHA（`base_commit`）；
   - 拒绝已存在的目标分支名或已存在的目标目录（绝不覆盖/劫持既有分支）；
   - 在目标仓外部默认父目录（`<repo.parent>/<repo.name>-hub-<task-id>` 或显式 `parent_dir`）执行 `git worktree add -b <branch> <worktree_path> <base_commit>`；
   - **开发依赖引导（`shared_env_links`）**：由于 `.venv` 与 `integrations/pi-acp/node_modules` 不在 Git 追踪中，允许白名单显式声明将主仓已验证的被忽略路径以相对/只读软链接入新 worktree（必须检查：目标在主仓存在、在 worktree 内**不属于 Git 追踪文件**、且 worktree 目标位置尚不存在，否则 fail-closed 拒绝）。
2. **审阅前后防篡改绊线（`snapshot_integrity`）**：
   - 在每次只读主控审阅 turn **开始前**和**结束后**分别采集 `{head_sha, tree_sha, status_porcelain}`；
   - 任何差异（哪怕新建一个未追踪临时文件、修改任何文件、或移动 `HEAD`）立刻判定 `REVIEWER_MUTATION_VIOLATION`，**直接拒绝该轮审阅结果并将任务置为 `blocked`**。
3. **人工显式清理（`worktree-cleanup`）**：
   - 自动化循环在任何终态（`ready_for_user_review` / `blocked` / `stopped` / `error`）都**绝不自动删除 worktree 或分支**，始终保留现场供人类审阅合并；
   - 仅人类显式调用 `hub_cli.py worktree-cleanup` 时才清理，且存在未提交改动或活动 `.worker.lock` 时默认拒绝。

### 4.4 只读 OS 边界与安全卫生（`hub_sandbox.py`）

- **双重只读防线**（绝不只靠提示词；具体策略已由 [h1-cli-spike.md](h1-cli-spike.md) 实测落定）：
  1. **OS 层写拒绝（按厂商实测边界区分，避免 Seatbelt 嵌套冲突）**：
     - **AGY / Claude**：由 ATO 外层施加 macOS 只读 `sandbox-exec` profile——全局 `(deny file-write*)`，仅放行 `/dev/null`、当次 `runtime_dir`（叠加 `pi_toolchain.write_exclusions`）以及开启原生 `session_resume` 时必需的该 CLI 原生会话目录（AGY 为 `$HOME/.gemini/antigravity-cli`，Claude 为 `~/.claude` / `~/.claude.json` / `/tmp/claude-<uid>`）；**目标主仓、隔离 worktree、`.git`、`.codex/tasks`、`runtime/trusted` 全部在 OS 层禁止写入**。AGY 不再依赖与 `--disable-slash-commands` 冲突的 `--mode plan`，也不与内建 `--sandbox` 叠套。
     - **Codex**：实测外层 `sandbox-exec` 会与 Codex 内建 Seatbelt 冲突导致连只读命令也失败，因此遵循 [agents.py](../agents.py) L72–73，采用 Codex 原生只读 Seatbelt（首轮 `--sandbox read-only`，续轮 `-c sandbox_mode="read-only"`），不叠套外层 `sandbox-exec`。
  2. **事后树完整性绊线**：无论哪家主控，每轮审阅前后均强制执行 `snapshot_integrity`，形成独立于 CLI 内部实现的第二重硬保障。
- **清洁环境与防泄漏**：
  - 子进程环境剥离 `PYTHONPATH`、`NODE_OPTIONS`、`DYLD_*`、`LD_*`、`CTASK_SESSION_REF` 及 ATO actor 凭据路径；
  - 私有 `stderr.log` 以 `0600` 写入、上限 1 MiB；公开回执与异常信息只输出固定错误码与证据路径，不回显环境变量或原始 stderr。

### 4.5 有界 `Work → Host Checks → Review → Fix` 状态机（`hub_loop.py`）

#### 4.5.1 审阅裁决契约（`REVIEW_VERDICT_SCHEMA`）
审阅器必须输出严格符合 Schema 的单个 JSON 对象（拒绝未知字段、重复 key、非有限数、截断文本）：

```json
{
  "verdict": "approve | request_changes | blocked",
  "summary": "一句到一段技术总结",
  "findings": [
    {
      "id": "F1",
      "severity": "blocker | major | minor | nit",
      "file": "relative/path.py",
      "lines": "L10-L25",
      "category": "security | correctness | architecture | tests | scope | docs",
      "title": "简明问题标题",
      "evidence": "指向源码/diff/日志的客观依据",
      "required_fix": "具体修复要求（仅 blocker 驱动返修）"
    }
  ],
  "checked_items": ["逐项核验清单"],
  "residual_risks": ["已知残余风险"]
}
```

**代码拥有的强制一致性校验（`validate_review_verdict`）**：
- 若 `verdict == "approve"` 但 `findings` 中存在任意 `severity == "blocker"` → **格式非法，fail-closed 拒绝**；
- 若 `verdict == "request_changes"` 但 `findings` 中 `blocker` 数量为 0 → **拒绝打回**（防止审阅器用 `nit`/`minor` 制造无意义返修循环）；
- `blocker_fingerprint = sha256(tree_sha + sorted((file, category, normalized_title) for blockers))`。

#### 4.5.2 确定性状态流转
```text
created
  │ 1. 供给隔离 worktree（已存在合法 worktree 绑定时直接复用）
  ▼
running_worker
  │ 2. 在 worktree 内通过未改动 workflow.py 运行 Worker（默认 Codex）与宿主独立 checks
  │    ├─ 任意 command 验收失败 / worker blocked / stopped / limit
  │    │    └─► 镜像状态并停止（绝不跳过失败的测试去叫审阅器，不浪费审阅 token）
  │    └─ 全部 command 验收通过（仅剩 manual 人工验收项）
  ▼
reviewing（round += 1）
  │ 3. 采集 snapshot_integrity(before) + diff_bundle + 宿主测试结果摘要 + 设计文档路径
  │ 4. 调用当前 HubAdapter.run_turn(role='review', read-only)
  │ 5. 校验 snapshot_integrity(after) == before；校验 REVIEW_VERDICT_SCHEMA 与一致性
  │    ├─ worktree 被改动 ──────────────────► blocked (REVIEWER_MUTATION_VIOLATION)
  │    ├─ 鉴权失败 / 超时 / JSON 非法 ──────► blocked（保留私有证据，不静默放行）
  │    ├─ verdict == "blocked" ─────────────► blocked（升级给人类）
  │    ├─ verdict == "approve" ─────────────► ready_for_user_review（写报告 + 更新 handoff-brief + 停机！）
  │    └─ verdict == "request_changes"
  │         ├─ round >= max_review_rounds ──► blocked (review_loop_exhausted)
  │         ├─ blocker_fingerprint 已出现 ──► blocked (repeated_blockers)
  │         └─ 否则 ────────────────────────► fixing
  ▼
fixing
  │ 6. 在**同一隔离 worktree** 创建定向修复 workflow：
  │    • 验收 `checks` 逐字节拷贝自原始冻结 `spec.json`（审阅器绝对不能删改测试！）
  │    • `sandbox` / `permissions` / `authorization` 保持原始边界不扩张
  │    • 仅将本轮 `severity == "blocker"` 的结构化条目作为修复任务 prompt
  └─► 回到 running_worker（执行修复 → 重跑全量宿主 checks → 进入下一轮 reviewing）
```

#### 4.5.3 权威与停点红线
- `ready_for_user_review` 是自动闭环的**硬终点**：此时已保存 `reviews/round-N.md`、宿主测试日志、精确 branch/commit 范围，并按既有通知策略提醒用户；
- 审阅通过**不等于** `acknowledge`、合并、推送或上线；用户复核后自行（或在交互会话授权后）执行 `acknowledge`、合并到 `main`、`git push` 与 `worktree-cleanup`。

---

## 5. 人类交互工作流与主控 Skill（`skills/ato-hub-orchestrator/SKILL.md`）

无论你当天在用 **AGY**、**Claude Code** 还是 **Codex** 的交互窗口聊天，均可通过同一份厂商中立 Skill 驱动这套闭环：
1. **需求对齐与定稿（交互窗口）**：你与当前交互主控讨论目标、写设计文档或任务 `spec.json` 并拍板；
2. **一键后台派发**：主控调用 `hub_cli.py task-create` + 后台运行 `hub_cli.py task-run`（在隔离 worktree 内自动跑 Codex + 宿主全量测试 + 只读审阅 + 最多 2 轮 blocker 返修）；
3. **自动唤醒与终审汇报**：`task-run` 在 `ready_for_user_review` 或 `blocked` 退出后唤醒当前交互窗口，主控读取 `reviews/round-N.md` 与 `state.json` 给你精简汇总（改了什么、测试结果、审阅结论、残余风险、branch/worktree 位置）；
4. **随时接管 / 切换**：
   - 想中途接管长期主控会话：跑 `hub_cli.py hub-attach-cmd <hub-id>` 进原生 CLI；
   - 想临时只跑一次审阅（即使代码是手写或手工跑 Codex 产出的）：跑 `hub_cli.py task-review-only <hub-id> <task-id>`；
   - 想把项目主控从 AGY 换成 Claude 或 Codex：跑 `hub_cli.py hub-switch-agent <hub-id> --kind claude --source "..."`。

---

## 6. 与 `acpx` 及 W3 历史资产的演进关系

- **与 `Aias00/acpx@4666eaa` 的关系**：H1 **不依赖、也不修改 `acpx`**，优先用 3 个薄原生适配器拿到“零中间层 + 原生交互 `attach` + 逻辑常驻”的最佳体验。由于主控层已经抽离为中立的 `HubAdapter` 协议，未来若需要接入第 4/5 款仅提供 ACP 的 Agent，或需要物理常驻长连接与工具级脱敏流观测，只需新增 `hub_adapter_acpx.py` 实现同一协议即可即插即用，`hub_ledger.py` 与 `hub_loop.py` 零改动。
- **与 W3 `agy-acpx-integration` 的关系**：已落实 W3 审计的 `REDESIGN_FROM_MAIN` 结论——保留精确仓库/合约范围与禁止模型越权的思想，放弃其旧循环依赖（30 对）、旧 `(allow default)` sandbox helper 及强绑定 `acpx_codex` 依赖链。
- **与 W2-1 / W2-2 的关系**：完全正交。W2-1 解决的是受管 Pi 协调端的生产凭据与出网隔离；H1 解决的是多厂商可换主控 + 隔离 worktree 执行 + 只读审阅返修闭环；二者互不侵入。

---

## 7. 前置能力实测 Spike 门禁（实现前必过，单独小预算授权）

在编写三个真实适配器代码前，须先完成一份实证记录 `docs/h1-cli-spike.md`（仅在 `/tmp` 新建极小合成仓库，含 1 个 3 行 `README.txt`，**不接触真实项目、不修改本机配置、总调用上限 ≤4 次极短只读 turn**）：

| Spike ID | 核验目标 | 通过标准 | 若不成立的 Fail-Closed 处理 |
| --- | --- | --- | --- |
| **S1：AGY 结构化输出** | 运行 `agy -p --mode plan --output-format json --json-schema <schema> --disable-slash-commands --print-timeout 60` | 确认精确 stdout JSON 结构、schema 输出位置、提取 `conversation_id` 的精确字段名及非 0 退出码行为 | 立即停下来调整 `hub_adapter_agy.py` 解析契约，不靠猜字段名硬写 |
| **S2：AGY 只读沙箱嵌套** | 对比 (a) 仅 `agy --sandbox --mode plan` 与 (b) 外层 macOS `sandbox-exec`（deny write worktree）+ `agy --mode plan`；故意发出“尝试写 `probe.txt`”的只读测试 prompt | 证明 OS 层实际阻止写入 worktree 且无 Seatbelt 嵌套崩溃，`git status` 保持干净 | 若外层 `sandbox-exec` 与 `--sandbox` 冲突，采用实测通过的单一 OS 边界；若两者均无法在 OS 层拦截写入，则 `os_sandbox_compatible=unsupported` 并暂停 AGY 自动审阅启用 |
| **S3：AGY 逻辑常驻与接管** | 用 S1 返回的 `conversation_id` 发起第 2 轮 `agy -p --conversation <id>`，验证记住第 1 轮随机代号；检查并发/嵌套调用（从 AGY 内部子进程调 `agy -p`）是否无锁冲突；核实 `agy --conversation <id>` 接管命令可用 | 第 2 轮精准复述第 1 轮代号，嵌套调用退出码 0 | 若 `--conversation` 在嵌套或 print 模式下受限，将 AGY 的 `session_resume` 诚实标为 `unsupported`，首版走 `fresh_only` + `handoff-brief.md` |
| **S4：Claude / Codex 续接对称性** | 各跑 1 次极小合成只读 resume（`claude -p --resume <id> --tools Read,Glob,Grep --json-schema ...` 与 `codex exec resume <id> -c sandbox_mode="read-only" --output-schema ...`） | 验证精确 `session_id` 捕获、只读生效与结构化 JSON 提取 | 任一家不通过则对应适配器的 `session_resume` 保持 `unsupported`，不虚标 |

---

## 8. 离线必过验收矩阵（H1-O1 ～ H1-O10）

所有离线测试必须使用临时 `ATO_HOME`/临时 Git 仓与合成假 CLI fixture，**严禁调用真实云端模型或读取本机真实认证**，要求全量 **0 失败 / 0 错误 / 0 跳过**：

| ID | 必测正反例 |
| --- | --- |
| **O1 契约与裁决** | `REVIEW_VERDICT_SCHEMA` 合法/非法形状；`approve` 含 `blocker` 拒绝；`request_changes` 零 `blocker` 拒绝；重复 key、NaN/Infinity、未知字段、flag 注入 `-model` 拒绝；`blocker_fingerprint` 确定性 |
| **O2 多主控同契约一致性** | 对 `agy` / `claude` / `codex` 三个适配器跑同一套 conformance 测试（假 CLI 覆盖：首轮、续轮、缺失/损坏 JSON、鉴权阻塞、超时、非 0 退出、`attach_argv`、`doctor` 不泄露凭据） |
| **O3 OS 只读沙箱与篡改绊线** | **真实 macOS `sandbox-exec` 子进程行为测试**：尝试往 worktree、`.git`、主仓或 `runtime/trusted` 写文件被 OS 拒绝；额外模拟篡改 worktree 文件/HEAD，验证 `snapshot_integrity` 触发 `REVIEWER_MUTATION_VIOLATION` 并立刻 `blocked` |
| **O4 隔离 Worktree 生命周期** | `provision` 基于固定 `base_commit` 建分支与目录；已存在分支/目录撞车拒绝；`shared_env_links` 仅链接未追踪依赖且拒绝覆盖追踪文件；`cleanup` 拒绝脏工作树和持锁工作树 |
| **O5 端到端有界闭环** | 合成 Worker + 真实宿主检查命令 + 合成 Reviewer：第 1 轮返回 `request_changes`（含 blocker）→ 自动在同 worktree 启动修复 workflow 且原始 `checks` 不变 → 第 2 轮返回 `approve` → 停在 `ready_for_user_review` 且未触发 `acknowledge`/merge/push |
| **O6 防死循环与异常隔离** | 达 `max_review_rounds` 停在 `blocked`；连续相同 `blocker_fingerprint` 提前停在 `blocked`；Reviewer 返回 `blocked` 立即升级；Worker 测试未通过时不调用 Reviewer；`STOP` 文件在运行中急停并证明进程组清理完成（或报 `cleanup_blocked`） |
| **O7 跨厂商换主控与滚动** | 同项目内 `agy → claude → codex` 连续切换：保留旧原生会话审计、绝不把 A 厂商 `session_id` 传给 B 厂商、新主控 prompt 含 `handoff-brief.md` 重建溯源；达 `max_turns` 自动 rollover 到新会话 |
| **O8 防泄漏与环境卫生** | 向环境与假 CLI stderr 注入唯一合成 secret sentinel，扫描 `hub.json`、`state.json`、`events.jsonl`、`reviews/`、`handoff-brief.md` 与 stdout 确认零泄漏；确认剥离 `CTASK_SESSION_REF` / `PYTHONPATH` / `NODE_OPTIONS` / `DYLD_*` |
| **O9 权限红线** | 审阅意见无法修改原始验收 `checks`、无法放宽 `sandbox`/`permissions`、无法签发 actor grant 或调用 `acknowledge`；拒绝相对/symlink 逃逸路径 |
| **O10 回归与架构门禁** | `.venv/bin/python scripts/run_tests.py` 全量 0 失败/错误/跳过；全量 `py_compile`；`scripts/check_architecture.py` 双向依赖 ≤8 且为历史 31 对子集；八核心 AST 与 [w2-core-ast.json](../tests/fixtures/w2-core-ast.json) 字节一致且新增 `hub_adapter_` 禁入断言；[cli-surface.json](../tests/fixtures/cli-surface.json) 55 命令零改动；`pyproject.toml` / `docs/README.md` / `docs/source-map.md` / 分发清单同步 |

---

## 9. 分阶段实施拆解（供设计批准后交单会话 Codex 推进）

完成第 7 节前置 Spike 并评审确认后，离线实现拆为 **3 次独立可审查提交**（基于最新 `main` 建单一独立 worktree，仅开一个 Codex 会话）：

1. **Commit 1（`H1-A`：中立契约、沙箱、隔离 Worktree 与持久账本）**
   - 模块：`hub_contract.py`、`hub_sandbox.py`、`hub_worktree.py`、`hub_ledger.py`
   - 验收：O1、O3（真实 `sandbox-exec` + 篡改绊线）、O4、O7 + 全量回归与 8 对架构门禁。
2. **Commit 2（`H1-B`：三厂商薄主控适配器与注册表）**
   - 模块：`hub_adapter_agy.py`、`hub_adapter_claude.py`、`hub_adapter_codex.py`、`hub_registry.py` + 假 CLI fixtures
   - 验收：O2（三厂商一致性套件）、O8（防泄漏与环境清洗）+ 全量回归与 8 对架构门禁。
3. **Commit 3（`H1-C`：有界执行-审阅-返修闭环、独立 CLI、Skill 与分发同步）**
   - 模块：`hub_loop.py`、`hub_cli.py`、`skills/ato-hub-orchestrator/SKILL.md` + `pyproject.toml` / `docs/source-map.md` / `docs/README.md` / 分发与八核心 `hub_adapter_` AST 禁入测试
   - 验收：O5、O6、O9、O10 全量回归。

---

## 10. 评审确认清单（请用户拍板）

1. **架构边界**：认同零改动八核心模块、零改动现有 55 个 `ctask` 命令、以独立 `hub_cli.py` + 10 个单向叶模块（新增 0 循环依赖）落地 H1 吗？
2. **长期主控模式**：认同采用「逻辑常驻（薄调度 + 原生 `resume_turn` + `handoff-brief.md` + `rollover` + 原生 `attach` 接管）」作为 H1 默认路线，同时保留 `acpx` 在同一 `HubAdapter` 协议下的后续扩展槽位吗？
3. **前置 Spike 授权**：设计评审通过后，是否授权我们紧接着执行第 7 节的 **最小只读 CLI Spike（S1–S4，隔离 `/tmp` 合成仓库，总计 ≤4 次极短只读模型调用，零改动本机配置与真实项目）** 以落定 `docs/h1-cli-spike.md`？
