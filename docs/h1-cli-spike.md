# H1 前置只读 CLI 能力实测 Spike 记录（S1–S4）

状态：**已完成（2026-11-22）**。对照设计文档：[hub-orchestrator-design.md](hub-orchestrator-design.md)（基线 `main@1dc32b3`）。

本次 Spike 经用户单独授权，全程在 `/tmp` 新建极小合成 Git 仓库（仅含 3 行 `README.txt`）内执行，**未触碰任何真实项目、未修改本机运行态或认证配置**，执行后立即清理临时目录。真实模型调用严格控制在 **4 次**（AGY 2 次、Codex 2 次；Claude 因本机未登录消耗 0 次模型调用）。

---

## 1. 实测环境与总表

- 平台：macOS（具备 `/usr/bin/sandbox-exec`）
- 实测本机 CLI 版本：
  - `agy`: `1.5.14`
  - `codex`: `codex-cli 0.171.0`
  - `claude`: `2.1.325 (Claude Code)`（`claude auth status` 返回 `loggedIn: false, authMethod: "none"`）
- 离线零模型预检：三家 CLI 的 `--version` 均可在只读 `sandbox-exec` 下以退出码 `0` 运行，且向合成 repo 写入 `probe.txt` 被内核拦截（`Operation not permitted`，退出码 `1`）。

| Spike ID | 测试对象 | 实测命令形态 | 实测结果 | 结论与对实现的强制约束 |
| --- | --- | --- | --- | --- |
| **S1** | AGY `1.5.14` 结构化输出 | `agy -p <prompt> --mode plan --output-format json --json-schema <schema> --disable-slash-commands --sandbox --print-timeout 90s` | `rc=0`（16.93s），`git status` 空；返回完整 JSON envelope，含 `conversation_id`、`status="SUCCESS"`、`structured_output`、`response`、`num_turns=1`、`duration_seconds`、`usage` | **通过**；同时抓到关键告警：`stderr` 输出 `warning: --mode plan has no effect while slash command expansion is disabled.`，故不能依赖 `--mode plan` 作为只读防线 |
| **S2 + S3** | AGY 外层 `sandbox-exec` 只读边界 + `--conversation <id>` 续接 + 嵌套调用 | `/usr/bin/sandbox-exec -f <ro.sb> agy -p <prompt> --conversation <id> --output-format json --json-schema <schema> --disable-slash-commands --print-timeout 90s`（`<ro.sb>` 全局 `(deny file-write*)`，仅放行 `/dev/null`、私有 `runtime_dir`、`$HOME/.gemini/antigravity-cli`） | `rc=0`（44.22s），`stderr` 空，`probe.txt` 不存在，`git status` 空；`num_turns=2`，精准召回第 1 轮记忆 token `ALPHA-7391`；AGY 尝试 `write_file` 与 `run_command` 均被 OS 内核拦截（`operation not permitted` / `failed to create PTY: operation not permitted`） | **通过**：外层 macOS `sandbox-exec` 对 AGY 极佳——文件读工具正常、写文件与 PTY 命令执行均被内核硬拒绝、会话持久化正常、从 AGY 交互会话内部嵌套调用无锁冲突 |
| **S4-Codex** | Codex `0.171.0` 首轮 + `exec resume` 只读续接 | 首轮：`codex exec --sandbox read-only -c approval_policy="never" --json --output-schema <schema> -o <out> -`；续轮：外层 `sandbox-exec` + `codex exec resume <thread_id> -c sandbox_mode="read-only" -c approval_policy="never" --json --output-schema <schema> -o <out> -` | 首轮 `rc=0`（16.1s），从 `thread.started` 捕获 `thread_id`，写入合规 JSON；续轮 `rc=0`（19.6s），精准召回首轮 token `GAMMA-3096` 与 sentinel，`probe.txt` 未创建；**关键发现**：外层 `sandbox-exec` 与 Codex 内置 Seatbelt 嵌套冲突，导致续轮内部连重读 `README.txt` 的命令也被阻塞 | **通过（需避开双层 Seatbelt）**：`hub_adapter_codex.py` 必须使用 Codex 原生只读沙箱（首轮 `--sandbox read-only`、续轮 `-c sandbox_mode="read-only"`），**不可再外包一层 `sandbox-exec`**（与 [agents.py](../agents.py) L72–73 一致），并强制配合事后 `snapshot_integrity` 绊线 |
| **S4-Claude** | Claude `2.1.325` 外层 `sandbox-exec` 只读调用 | `/usr/bin/sandbox-exec -f <ro.sb> claude --safe-mode --restricted --disable-slash-commands -p --output-format stream-json --verbose --permission-mode dontAsk --tools Read,Glob,Grep --allowedTools Read,Glob,Grep --strict-mcp-config --mcp-config '{"mcpServers":{}}' --session-id <uuid> --json-schema <json>` | `rc=1`（0.87s），`total_cost_usd=0`，`git status` 空；`stream-json` 终态 `result` 返回 `is_error=true, structured_output=null`；`claude auth status` 确认 `loggedIn=false` | **离线/鉴权阻塞路径已实证**；因本机未登录 Claude，实网 `structured_output` 与 `session_resume` 保持 `unknown`（不得虚标 `supported`），离线实施使用合成假 CLI fixture 完整覆盖其协议契约 |

---

## 2. 实证协议契约（供 H1-B 适配器直接对齐）

### 2.1 AGY（`hub_adapter_agy.py`）
- **推荐启动参数**（不组合 `--mode plan` 与 `--disable-slash-commands`，不与外层 `sandbox-exec` 叠用内置 `--sandbox`）：
  - 外层包装：`/usr/bin/sandbox-exec -f <ro.sb>`（`(deny file-write*)`，仅放行 `/dev/null`、私有 `runtime_dir`、`write_exclusions` 保护，以及当 `session_mode` 启用持久化时放行 `$HOME/.gemini/antigravity-cli`）
  - 内层命令：`agy -p <prompt> --output-format json --json-schema <schema_file> --disable-slash-commands --print-timeout <t>s [--conversation <id>] [--model <m>] [--effort <e>]`
- **已验证 stdout JSON envelope 字段**：
  - `conversation_id`（非空字符串，作为 `HubTurnResult.session_id`）
  - `status`（必须精确等于 `"SUCCESS"`，否则 fail-closed）
  - `structured_output`（JSON 对象，经 `hub_contract` 严格校验）
  - `num_turns`、`duration_seconds`、`usage`（脱敏写入 metadata）
- **交互接管与锁位置**：会话数据保存在 `$HOME/.gemini/antigravity-cli/{conversations/<id>.db, presence/<id>.lock, brain/<id>/}`，人类可通过 `agy --conversation <id>` 接管；`HubCapabilities` 中 `structured_output`、`session_resume`、`interactive_attach`、`os_sandbox_compatible` 具备本机实测证据，`read_only_cli` 在禁用 slash commands 时不单独宣称有效（依赖外层 OS 沙箱 + `snapshot_integrity`），`long_lived_stream` 为 `unsupported`。

### 2.2 Codex（`hub_adapter_codex.py`）
- **推荐启动参数**（遵循 [agents.py](../agents.py) L72–73：使用 Codex 原生 Seatbelt，不叠套外层 `sandbox-exec`）：
  - 首轮：`codex exec --sandbox read-only -c approval_policy="never" --json --output-schema <schema_file> -o <output_file> [--model <m>] -`
  - 续轮（`cwd=<worktree>`）：`codex exec resume <session_id> -c sandbox_mode="read-only" -c approval_policy="never" --json --output-schema <schema_file> -o <output_file> [--model <m>] -`
- **已验证输出与会话 ID 捕获**：
  - 从 stdout JSONL 的 `type == "thread.started"` 事件提取 `thread_id` 作为 `session_id`；
  - 从 `-o <output_file>` 读取最终 JSON 对象并经 `hub_contract` 严格校验；
  - 交互接管命令：`codex resume <session_id>`；
  - 强制配合 `hub_worktree.snapshot_integrity` 事后篡改绊线。

### 2.3 Claude Code（`hub_adapter_claude.py`）
- **启动参数**：对齐 [agents.py](../agents.py) L287–296（外层 macOS `sandbox-exec` 放行 `runtime_dir`、`~/.claude`、`~/.claude.json`、`/tmp/claude-<uid>` + `--safe-mode --restricted --disable-slash-commands -p --output-format stream-json --verbose --permission-mode dontAsk --tools Read,Glob,Grep --allowedTools Read,Glob,Grep --strict-mcp-config --mcp-config '{"mcpServers":{}}' --json-schema <json>`）；
  - 无状态模式附带 `--no-session-persistence --session-id <uuid>`；
  - 持久化首轮附带 `--session-id <uuid>`，续轮改用 `--resume <session_id>`。
- **鉴权阻塞识别**：实测确认未登录时 `rc=1` 且 `stream-json` 终态 `result` 含 `is_error=true, structured_output=null, total_cost_usd=0`；适配器应结合 `stderr` 与终态 `result` 文本（以及 `claude auth status` 只读探测）将其准确归类为 `auth_blocked`，不消耗重试或误报成功。
- **诚实能力声明**：在完成真实已登录样本验证前，`hub_adapter_claude.py` 对 `session_resume` 保持代码拥有静态声明为 `unknown`（由 `resume_if_supported` 自动采用 `fresh_only` + `handoff-brief.md`，绝不虚标）。

---

## 3. 对设计文档的修订核对

基于本次 Spike 实测结果，已同步收敛 [hub-orchestrator-design.md](hub-orchestrator-design.md) 中的两处边界细节（无架构方向变更，八核心零改动、0 新增循环依赖与 3 提交拆解保持不变）：
1. AGY 只读模式统一采用「**ATO 外层 macOS `sandbox-exec` + `--disable-slash-commands`（不依赖 `--mode plan` 与 `--sandbox` 嵌套）+ 事后 `snapshot_integrity`**」；
2. Codex 只读模式统一采用「**Codex 原生 Seatbelt（`--sandbox read-only` / `-c sandbox_mode="read-only"`，不外包双层 `sandbox-exec`）+ 事后 `snapshot_integrity`**」。

本次 Spike 门禁通过，可以按设计文档第 8–9 节启动离线实现（H1-A / H1-B / H1-C，全程仅用假 CLI fixture，不调用真实模型）。
