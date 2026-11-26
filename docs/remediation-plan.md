# Agent Task Orchestrator (ATO) 整改方案

本文档面向工程维护与 Codex 自主改造，基于主线提交 `f7283bd` 的源码审计结果制定。整改按 **P0 → P1 → P2 → P3** 分阶段推进，优先修复确定性缺陷、文档失真与安全边界不一致，再进行架构解耦与长期能力补齐。

---

## 0. 全局红线与不可破坏边界（执行前必读）

任何整改改动必须严格遵守 [AGENTS.md](../AGENTS.md) 与现有测试契约：

1. **不触碰本机生产运行态**：不得替换或启动 `~/.local/bin/ctask`、本机 controller/supervisor、真实 hub、真实原生会话或真实模型 provider；不得提交 `.codex/tasks/`、凭据、SQLite 状态或 `operator-evidence/`。
2. **保留核心安全不变量**：
   - 不可变合约与 `contract_hash` 校验不变；
   - `STOP` 与 `user-closure.json` 最高优先级不变；
   - 独立宿主验收（`checks` argv）不变，模型输出或 ACP `end_turn` 永不替代验收；
   - `delivery_unknown` 隔离且绝不自动重发/换 ID 重试；
   - 累计尝试次数、阶段数、预算账本和已投递历史不可重置或改写。
3. **保留既有架构与分发测试硬约束**：
   - `tests/test_codex_session_adapter.py::test_core_ast_has_no_vendor_imports_or_native_event_decoders` 检查 `recovery.py` 及 `ato_workflow/` 下的 `control`、`interaction`、`delegation`、`workflow`、`session_router`、`message_bus`、`transport`。这八个核心模块通过注册表和会话路由访问适配能力，禁止直接导入原生适配器。
   - `tests/test_source_distribution.py` 强制要求分发脚本中不得出现 本机用户目录或挂载目录的绝对路径；
   - `tests/test_compact_handoff.py` 强制要求紧凑宿主交接通知长度 `< 260` 且包含 `ato-read-handoff <id>`；
   - 禁止静默 skip 测试；离线 fixture 通过不得宣称为真实 CLI 或云端模型验收通过。

---

## 1. 本地测试环境准备（Codex 启动前执行）

在仓库根目录独立创建隔离依赖环境，使全部 602+ 离线回归测试均可运行（0 failures, 0 errors, 0 skips）：

```sh
cd /path/to/agent-task-orchestrator
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-acp.lock
npm ci --ignore-scripts --no-audit --no-fund --prefix integrations/pi-acp
.venv/bin/python scripts/run_tests.py
```

> 注意：macOS 默认 `python3` 可能为系统 3.9，所有命令必须使用 `.venv/bin/python` 或 `python3.12`。不要提交 `.venv/` 与 `integrations/pi-acp/node_modules/`（均已 gitignore）。

---

## 2. 阶段 P0：低风险缺陷修复与文档/测试入口纠偏（优先执行）

目标：以最小改动消除明确的代码 bug、守护进程隐蔽风险与 README 失真，保持对外接口完全兼容。

### P0-1 修正 `README.md` 过时与失真段落
- **现状问题**：
  - [README.md](../README.md) 第 303 行引用了不存在的 `message_adapters.py`；
  - 第 312–322 行（*Agent-neutral workflow routing (feature checkout)*）引用了 `runner.py` 中不存在的 `mailbox-register`、`workflow-create --origin-endpoint`、`mailbox-pump`（与 [message-protocol.md](message-protocol.md) 第 17 行矛盾）；
  - 第 325–336 行（*Offline upgrade and rollback*）引用了不存在的 `scripts/upgrade_runtime.py`（现行流程见 [runtime-upgrade.md](runtime-upgrade.md)）。
- **改造要求**：
  1. 将上述三处对齐 `main` 分支真实模块名（`transport.py`、`session_router.py`、`agent_registry.py`、`local_session_adapter.py`、`pi_session_coordinator.py`）和真实 CLI 子命令（`session-register`、`local-session-create`、`workflow-create --origin-ref`、`scripts/install.py`）。
  2. 明确指向 [source-map.md](source-map.md)、[message-protocol.md](message-protocol.md)、[runtime-upgrade.md](runtime-upgrade.md) 与 [development-status.md](development-status.md)。
- **验收标准**：
  - `README.md` 中不再出现 `message_adapters.py`、`mailbox-register`、`mailbox-pump`、`--origin-endpoint`、`upgrade_runtime.py`；
  - 仓库内所有 Markdown 相对链接 0 失效。

### P0-2 修复 `workflow-create --origin-ref --auto-recover` 参数校验缺陷
- **现状问题**：
  - `runner.py` 第 891 行写死 `if args.auto_recover and (not args.origin_thread or not args.recovery_source):`，拒绝了配合 `--origin-ref` 启用 `--auto-recover`，但底层 `recovery.enable()`（`recovery.py:52-70`）已支持 `origin_session_ref`。
- **改造要求**：
  1. 将校验改为 `if args.auto_recover and (not (args.origin_thread or args.origin_ref) or not args.recovery_source):`，并更新错误信息表述为需要已验证的 `--origin-thread` 或 `--origin-ref` 及 `--recovery-source`。
  2. 检查 `recovery.enable()` 在仅提供 `origin_ref`（无 `origin_thread`）时的落盘字段（如 `codex_home`、绑定校验与 `summary()`），确保全链路可用。
- **验收标准**：
  - 新增单测覆盖通过 CLI 执行 `workflow-create --origin-ref <ref> --auto-recover --recovery-source <src>` 成功启用监督，且无 origin 或无 source 时仍拒绝；
  - 旧的 `--origin-thread --auto-recover` 回归测试全部通过。

### P0-3 加固 `scripts/run_tests.py` 的版本与依赖前置诊断
- **现状问题**：
  - `scripts/run_tests.py` 未检查 `sys.version_info < (3, 12)`，在系统 Python 3.9 下爆出 60+ 条晦涩的 `TypeError: unsupported operand type(s) for |`；
  - 缺少可选 `agent-client-protocol` 或 `integrations/pi-acp/node_modules/pi-acp` 时，ACP 相关测试报成难以定位的 `blocked` 或 `FileNotFoundError`。
- **改造要求**：
  1. 在 `scripts/run_tests.py` 入口首先校验 `sys.version_info < (3, 12)`，不满足时打印清晰提示并 `return 2`。
  2. 运行前探测 `importlib.util.find_spec('acp')` 与 `integrations/pi-acp/node_modules/pi-acp/dist/index.js` 是否存在；缺失时在 stderr 打印清晰的安装指引（`.venv` + `pip install -r requirements-acp.lock` + `npm ci --prefix integrations/pi-acp`）。
  3. **严禁**因为依赖缺失而 skip 测试或返回 0；仍应如实暴露失败或通过明确参数（如默认 fail-fast 返回非 0，或打印前置缺失清单后执行/退出）。
- **验收标准**：
  - 新增单测验证低版本提示逻辑与缺失依赖诊断输出，且不产生任何 skipped 用例。

### P0-4 消除 `coordination.py` 中 `READER` 的写死路径
- **现状问题**：
  - `coordination.py:15` 硬编码 `READER = str(Path.home()/'.local/bin/ato-read-handoff')`。若通过 `scripts/install.py --bin-dir /custom/bin --helpers` 安装，生成的交接唤醒指令会指向错误路径。
- **改造要求**：
  1. 将 `coordination.py` 的 reader 解析改为函数 `reader_command()`：
     - 优先使用受信任的当前安装位置：如通过 `scripts/install.py` 安装的同目录 `ato-read-handoff`（或在显式配置 `CTASK_HANDOFF_READER` 且指向现有可执行文件时使用），默认回退为 `~/.local/bin/ato-read-handoff`（保持兼容且不引入本机用户目录硬编码）。
     - 同步检查 `scripts/install.py` 生成 `ctask` / `ctask-notify` wrapper 时，若与 `ato-read-handoff` 同目录，可安全关联。
  2. 严格保持紧凑通知长度 `< 260` 字节，且保留 `[ctask-host-handoff id=...]` 与 `ato-read-handoff <id>` 格式。
- **验收标准**：
  - `tests/test_compact_handoff.py` 与 `tests/test_source_distribution.py` 全部通过；
  - 新增单测验证自定义 `--bin-dir` 安装场景及默认回退行为。

### P0-5 加固守护进程（`_controller` / `_supervisor`）存活校验、异常容错与 `generation()` 性能
- **现状问题**：
  1. `control.py:888` 和 `recovery.py:637` 仅凭 `os.kill(pid, 0)` 判断守护进程是否存活，未校验 `runner.process_identity(pid)`，进程被 SIGKILL 后若发生 PID 复用会误判存活而拒绝启动；
  2. `control.py:909-915` 与 `recovery.py:658-667` 的主循环遇到任意瞬态异常（如临时锁争用、短暂 IO 抖动）会直接永久退出 `state='exited'`；
  3. `recovery.py:127-140` 的 `generation()` 每次调用全量读入并解析整个 `events.jsonl`。
- **改造要求**：
  1. `process.json`（controller）与 `supervisor.json`（recovery）在启动及心跳更新时记录 `pid_identity = core.process_identity(pid)`；存活检测改为 `pid_alive(info)`：先查 `alive(pid)`，若记录了 `pid_identity` 则同时核对当前 `process_identity(pid)` 一致（兼容旧格式无 `pid_identity` 字段的记录）。
  2. 在 `_controller` 与 `_supervisor` 主循环中对单次 `tick()` 的瞬态异常增加有界连续失败计数与指数/线性退避（例如连续失败 N 次才退出，每次将错误信息写入状态文件与日志，成功后计数清零）；绝不吞掉 `KeyboardInterrupt`/`SystemExit`，也不改变 `delivery_unknown` 不重发语义。
  3. 优化 `recovery.generation()`：从文件尾部按块反向查找最近一条 `workflow_started` 事件（文件较小时直接解析，大文件反向按行扫描，遇到首个合法 `workflow_started` 立即返回），保持返回值 `[last, execution_revision]` 与损坏行容错行为 100% 一致。
- **验收标准**：
  - 新增单测覆盖：PID 复用伪造（相同 PID 但不同 `pid_identity` 允许重新拉起）、`tick()` 瞬态异常后自恢复、连续超限后安全退出并记录 `error`、大体积 `events.jsonl` 下 `generation()` 与全量扫描结果一致且能正确跳过末尾损坏 JSON 行。

---

## 3. 阶段 P1：安全边界对齐与 ACP 排障可观测性

目标：修补 Worker 与 Coordinator 的文件访问隔离缺口，让 ACP Worker 在不泄露凭据的前提下具备基本可排障能力。

### P1-1 对齐 Worker 与 Coordinator 的敏感路径保护
- **现状问题**：
  - `pi-path-guard.mjs:14-16` 仅对 `edit`/`write` 拦截 `.git` 和 `.codex`，未阻止 Pi 文件 worker **读取**仓库内的 `.git`、`.codex`、`.ato`、`.pi`、`.aws`、`.ssh`、`.env`；
  - `agents.py:247-250` 的 `sandbox_profile()` 对 `workspace-write` 仅排除了 `.git` 与 `.codex` 的写入，未同步排除 `.ato`、`.pi`、`.aws`、`.ssh`、`.env`。
- **改造要求**：
  1. 统一敏感路径段黑名单（`.git`, `.codex`, `.ato`, `.pi`, `.aws`, `.ssh`, `.env`）：
     - 在 `pi-path-guard.mjs` 中对所有文件工具（含 `read`、`grep`、`find`、`ls`、`edit`、`write`）拦截目标路径进入上述敏感目录/文件（注意：当 `ls`/`find`/`grep` 以仓库根 `.` 调用时需妥善处理，不可误杀正常源码检索，同时禁止显式指向 `.env`/`.git`/`.codex` 等路径）。
     - 在 `agents.py` 的 `sandbox_profile()` 写入规则中同步排除 `.ato`、`.pi`、`.aws`、`.ssh`、`.env`。
  2. 核查 `agents.py:240-246` 传统 Claude/Pi worker 沙箱对 `~/.claude` / `~/.pi/agent` 的 OS 写权限，在 `agents.doctor()` 的 `worker_guard` 说明中如实标注其与 `acp_pi` 只读凭据隔离的差异（不破坏已有登录可用性）。
- **验收标准**：
  - `tests/test_agents.py` 现有沙箱用例全绿；
  - 新增 Node/Python 单测验证 `pi-path-guard.mjs` 拒绝读取和写入 `.env`、`.git/config`、`.codex/tasks/state.json`、`subdir/.env`，同时允许正常源码的 `read`/`ls`/`grep`/`edit`/`write`。

### P1-2 为 ACP Worker 增加私有 `stderr.log` 排障落盘（不泄密）
- **现状问题**：
  - `acp_bridge.py:111` 直接把子进程 `stderr` 丢弃到 `DEVNULL`，`pi_acp_worker.py:59-62` 只输出异常类名，导致真实故障完全黑盒。
- **改造要求**：
  1. 在 `acp_bridge.invoke()` 中增加可选参数 `stderr_path=None`；当由 `pi_acp_worker.py` 调用时传入 `runtime/'stderr.log'`（位于 `0700` 私有目录，文件权限 `0600`，单次截断/有界落盘，例如上限 1 MiB）。
  2. `pi_acp_worker.py` 捕获异常时仍不向父进程 stdout/stderr 打印原始协议正文或凭据，仅打印安全错误类型 + 私有诊断路径提示（如 `ACP Pi worker failed: ValueError (inspect private runtime stderr at ...)`）。
  3. 确保 `task_trace.py` 仍保持纯元数据，不包含 stderr 原文。
- **验收标准**：
  - `tests/test_acp_scheduler_integration.py` 与 `tests/test_task_trace.py` 全绿；
  - 新增单测验证 ACP 子进程写入 stderr 后私有日志以 `0600` 落盘，且父 stderr / `task_trace` 中不泄露敏感字符串。

---

## 4. 阶段 P2：架构解耦、CLI 模块化与仓库/文档治理

目标：显著降低循环依赖和巨型函数复杂度，改善工程可维护性，同时**100% 保留原有公共模块名、CLI 表面和 AST 边界约束**。

### P2-1 抽出零依赖底层原语模块，打破循环依赖
- **现状问题**：
  - 36 个根模块存在 31 对双向循环依赖、109 处函数内延迟 `import`，核心根源是 `runner.py` 既承担最底层工具函数（`now`/`read`/`save`/`digest`/`process_identity`/`Halt`），又承担最顶层 CLI 调度与各子系统入口；`recovery.py` 同时承担通用文件锁 `locked()`/`alive()` 与业务监督逻辑。
- **改造要求**：
  1. 新增无业务依赖的基础模块 `ato_core.py`（仅依赖 Python 标准库），沉淀：
     - `now`、`read`、`save`、`digest`、`process_identity`、`Halt`、`locked`、`alive`、`pid_alive`；
     - 在 `runner.py` 和 `recovery.py` 中直接 re-export 上述符号，保持所有既有外部调用与测试的 `runner.read`、`recovery.locked` 等 API 完全兼容。
  2. 逐步将各子模块对 `runner`、`recovery` 底层工具的依赖切换至 `ato_core`，清理多余的局部延迟 `import`。
  3. 将 `runner.py` 中 352 行的 `main()` 按领域拆分至内部子模块目录 `ato_cli/`（例如 `workflow_cmd.py`、`budget_cmd.py`、`interaction_cmd.py`、`control_cmd.py`、`recovery_cmd.py`、`session_cmd.py`），`runner.py` 保留 `main()` 作为轻量聚合分发入口。
  4. **严格遵守 AST 边界**：`ato_core.py` 与 8 个受保护核心模块不得导入 `codex_legacy`、`appserver`、`codex_session_adapter`。
- **验收标准**：
  - 602+ 全部既有单测零修改全绿；
  - 新增结构测试校验核心底层模块（`ato_core`、`ato_config`、`agent_registry`）无反向业务导入，并将双向循环依赖数量显著压降（目标从 31 对降至 ≤ 8 对，且不新增任何循环）。

### P2-2 工程打包元数据与文档/历史脚本整理
- **现状问题**：
  - 缺少 `pyproject.toml`；
  - `docs/` 下 47 份文件新旧混杂，历史阶段 checkpoint 文档与现行设计混放；
  - `scripts/prepare_*_candidate.py` 4 个历史内部脚本硬编码指向 gitignored 的 `operator-evidence/`，外部直接调用报错含糊。
- **改造要求**：
  1. 新增轻量 `pyproject.toml`：声明 `requires-python = ">=3.12"`、核心零第三方依赖、`[project.optional-dependencies] acp = [...]`（与 `requirements-acp.lock` 一致），以及元数据说明（不替代 `scripts/install.py` 的无副作用本地安装边界）。
  2. 新增 `docs/README.md` 索引目录，将文档明确分类为：**现行架构与操作指南**、**能力边界与状态**、**历史阶段验收档案（只读参考）**（先做索引分类，避免盲目移动文件导致既有文档间链接或引用失效）。
  3. 为 4 个 `scripts/prepare_*_candidate.py` 在缺失 `operator-evidence/` 输入时补充清晰的人类可读提示，说明其属于历史内部验收打包脚本、非源码安装或日常测试步骤。
- **验收标准**：
  - `python3.12 scripts/run_tests.py` 全绿；
  - 全仓库 Markdown 相对链接校验 0 失效；
  - 4 个 `prepare_*_candidate.py` 在无 `operator-evidence/` 情况下返回清晰错误说明（无未处理 traceback）。

---

## 5. 阶段 P3：环境解耦与长期能力演进（需单独评审后分任务实施）

本阶段涉及协议/运行边界变更，建议在完成 P0–P2 后，按独立任务合约逐项设计与验收：

### P3-1 解除 Pi/Node 路径硬编码与 `pi-acp` 文本补丁脆弱性
- **问题定位**：`pi_session_profile.py`、`pi_acp_worker.py`、`scripts/derive_pi_acp.py` 写死 `~/.nvm/versions/node/v24.22.1/bin/node`、Pi `1.5.2` 和 `pi-acp@0.0.34` 字符串替换。
- **演进方向**：
  1. 引入受信任本机配置解析器（如校验 `CTASK_NODE_BIN` / `PATH` 中的 Node 主版本 >= 24 且可执行，而非写死 nvm 单一目录）；对 Pi 与 `pi-acp` 保留显式版本与哈希白名单表，支持多版本受控升级而不必改散落常量。
  2. 将 `pi-acp` 补丁点封装为清晰的协议适配层或向 `pi-acp` 上游推进原生 `ctaskTurn` / native receipt 扩展支持，逐步淘汰 `replace_once` 源码注入。

### P3-2 多 Agent 协调闭环与跨平台沙箱路线
- **Pi Coordinator 真实 Provider Profile**：在保留 `LocalPiProfile` 回环测试隔离的基础上，设计独立的 `ProductionPiProfile`（明确凭据只读挂载、出口域名白名单/网络策略、真实模型资格验证清单，与离线 fixture 分开验收）。
- **Pi 原生恢复（`session.load_resume`）**：补充受管 Pi 会话原生加载的代次与幂等证明，验证通过后再将能力声明从 `unsupported` 升级为 `supported`。
- **Claude Coordinator Transport**：若需支持无 Codex 依赖的 Claude 协调端，需基于 `agent_registry` v2 契约补齐独立受管传输与工具守卫，不可复用文件 worker 直接冒充协调端。
- **Linux 沙箱后端**：为外部 CLI worker 设计基于 Linux namespace / `bubblewrap` 的等价文件写隔离，解除对 macOS `sandbox-exec` 的唯一绑定。
- **未合并候选分支消化**：按 [development-status.md](development-status.md) 逐个审查 `origin/work/local-candidate-snapshots-20261120` 中的 `tracking-rectification`、`execution-efficiency`、`agy-acpx-integration`，通过独立源码评审与新验收合入，不直接覆盖主线。

---

## 6. 给 Codex 的执行批次与提交建议

建议按以下 4 个独立批次执行，每批次完成后立即跑完验收命令再进入下一批：

| 批次 | 包含项 | 预计风险 | 必跑验收命令 |
| --- | --- | --- | --- |
| **Batch 1** | P0-1, P0-2, P0-3, P0-4 | 极低 | `.venv/bin/python scripts/run_tests.py` + Markdown 链接检查 |
| **Batch 2** | P0-5, P1-1, P1-2 | 低 | `.venv/bin/python scripts/run_tests.py` + 新增单测专项检查 |
| **Batch 3** | P2-1, P2-2 | 中（结构重构） | `.venv/bin/python scripts/run_tests.py` + AST 边界测试 + 循环依赖对比检查 |
| **Batch 4** | P3-1（先出方案/单独确认后实施） | 中高 | 独立设计文档 + 隔离验证，P3-2 按需求另建总任务合约 |

### 每批次最终完成检查清单（Definition of Done）
- [ ] `.venv/bin/python scripts/run_tests.py` 全部通过（0 failures, 0 errors, 0 skips）
- [ ] `python3.12 -m py_compile` 全量源码与测试无语法错误
- [ ] `test_core_ast_has_no_vendor_imports_or_native_event_decoders` 与 `test_source_distribution.py` 通过
- [ ] `git status` 干净，不含 `.venv/`、`node_modules/`、`.codex/tasks/`、临时日志或私有凭据
- [ ] 未改动本机 `~/.local/bin/ctask` 或正在运行的 controller/workflow
