# Agent 无关消息层：本次后台任务约定

## 完整目标

将 ctask 的会话间通信改成「统一持久化消息箱 + Agent Adapter」。Codex queue 是一个投递适配器；Claude Code 和 Pi 可作为框架管理的独立执行/协调端点，能接收后续消息、回复、提出需要用户处理的问题并接收实际答复。不能只添加两个 worker 名称或只画架构图即结案。当前用户交互入口可继续为 Codex，内部身份和存储不要求每个 origin 都是 Codex UUID。

用户明确授权本次后台本地实现和测试，原文「这个也放到后台长任务去执行吧」，所指是刚讨论的统一消息箱和三类 CLI 适配。不使用 goal。本次在已发布仓库的 feature branch 上实现，使用外部稳定执行器运行，避免修改正在驱动自己的运行时。

## 范围和权限

- 源码工程是当前仓库。可改本地源码、测试、文档、仓库内 skill，提供安装、数据兼容/迁移和回退工具。不得修改 `.codex/tasks` 中固化计划、验收脚本或基线测试。
- 不触碰实际运行部署目录、真实 hub/任务数据库、用户账号配置、Drover、Stream Agent 或生产服务器。不提交/推送、不部署、不向真实其他会话发送探测消息。现有稳定运行时继续运行。
- 主控和后台 origin 仍是当前已核实 Codex 会话；任务实现的适配器可以有非 Codex origin。旧 Codex 绑定/JSON 和 CTASK_QUEUE_BIN fixture 行为向后兼容，不用第三方 ID 覆盖旧 origin UUID。
- 不依赖用户新增登录或购买额度来验证本地协议；原生 CLI 使用隔离配置和仅本地假 provider，使用明确虚构凭据，不复制/输出真实 key。不得通过读取原生认证文件并写入任务记录实现适配。没有真实账号验证只能如实标明，不得称云端认证通过。
- 第三方功能限定框架管理的 CLI 会话，不声称能注入任意已有 TTY/交互窗口；由显式注册的身份和工作目录确定来源。工具权限不比当前文件工具 worker 更宽；不因「协调会话」开放 Bash、MCP 或子 agent。

## 功能与可靠性

1. SQLite 持久化通用 endpoint 和消息 envelope。至少包含消息 ID、来源/目标、kind、关联/回复 ID、任务/合约来源、状态与时间；正文和凭据分离。来源可核实，跨工程/会话错配拒绝。消息进入存储再触发适配器，数据库不假装提供模型唤醒。
2. 注册/出队/接收确认/回复/超时/取消分别记录。幂等键、事务并发、单端点串行/排队、忙碌、崩溃恢复、重复回执、过期与迟到回执均有真实测试。claim/lease 可恢复未发送工作，但已可能产生外部效果的投递不盲目重发，不宣传 exactly-once。
3. Codex Adapter 封装 queue/app-server 所需特有逻辑，现有 hub/ask/answer/accept-answer、监督、待办和 delegate 兼容。核心 interaction/control/recovery/delegation 不直接拼接 codex queue；通过统一投递接口处理。
4. Claude Code 使用实际安装版本的 stream-json 输入/输出或显式原生 resume，Pi 使用 RPC 或显式 session。每个框架管理端点具备生命周期、独占所有权和可解释的恢复能力；同一会话至少两轮请求与回复关联正确，不能靠每条重新开空白会话伪装上下文续接。所有外部响应归一化为通用事件。
5. 支持 Codex hub 与非 Codex origin 的提问/实际答复往返，完成/阻塞/无进展等事件汇入同一主控待办。来源、用户许可、接收和真实执行仍分离；STOP、过期、合约变化、用户暂缓后不恢复或催促。原消息/旧表不可丢失，不读取完整历史来猜身份。
6. 持续保留旧累计预算、失败与接管记录。此任务不实现通用自动预算追加、不扩大已有任务范围；也不增加跨机器、开机自启、Web UI 等新目标。

## 必须交付和验证

- 新功能测试模块按以下路径组织：`tests/test_message_bus.py`（存储/状态/并发/故障），`tests/test_message_adapters.py`（三执行端与生命周期），`tests/test_agent_mailbox_integration.py`（混合 origin/hub、提问/回答、监督/待办与旧数据）。测试是实际行为验证，不匹配实现文本或只断言布尔值。
- 宿主保留并运行发布时的120项基线测试副本，原断言和测试源独立保留。新源码同时运行当前完整测试套件。不得删旧测试/降低断言/把错误改 skip。
- `scripts/verify_native_mailbox.py --output PATH` 由宿主运行，在临时工程/隔离 CLI 配置和本地模拟 provider 下驱动已安装真实 Claude/Pi；不连接真实 hub、不写现有会话。必须验证两轮关联/会话连续性和结果返回，不只 --version。
- 原生探针输出 JSON：`schema_version: 1`，`runs` 包含每个 kind `claude` / `pi`，每项有 `passed: true`、`executable`、`executable_sha256`、`version`、`request_count >= 2`、`response_count >= 2`、`same_session: true`、`provider: "local-fixture"`、`real_cli: true`。为有意义的日志/收据提供路径，不保存真实凭据。宿主独立核对可执行文件身份/hash；报告不是替代真实运行的许可。
- 提供 `scripts/upgrade_runtime.py --dry-run --runtime PATH`，宿主会对临时旧部署快照运行并核实原文件未改；准备明确安装/数据兼容与回退方案，本次不激活真实本机运行时。旧持久化数据在临时fixture中迁移/重启回归。
- 更新 `docs/message-protocol.md`、`docs/agent-transports.md`、架构/README/skill，区分真实实现、fixture验证和云端账号前提，不把支持范围夸大。产出源版本/最终验收/安装步骤的交接文件 `build/message-transport/handoff.md`。

## 阶段和估时

已发布版本120项本地完整测试实测83.52秒，已安装 Codex0.165.0 / Claude2.1.325 / Pi1.5.2；Claude未登录，Pi原生RPC文档可本地读取。这是已核实依据，不是未来所有检查的耗时保证。

| 剩余工作 | 条件式估计 |
| --- | ---: |
| 消息协议、存储生命周期与并发/故障测试 | 15–25分钟 |
| Codex/Claude/Pi adapter 与受管端点 | 30–50分钟 |
| 旧交互/监督/待办接入、兼容数据 | 20–35分钟 |
| 原生本地端点探针、完整回归与修复 | 15–25分钟 |
| 安装迁移/回退工具、文档和交接 | 10–15分钟 |
| 小计 | 90–150分钟 |

执行硬上限180分钟，含30分钟上界余量；监督210分钟，额外30分钟仅观察/路由。最多2次自动修复接管，同类失败最多2次；接管修复须在同一工作账本内核算，不另给无限时间。执行预算由旧执行器累计计时约束，本任务还保留绝对停止保护以覆盖 origin 前台修复，不因重建/续办清零。

前20分钟应确定协议和原生CLI通道；第60分钟应已有至少持久化消息与一种非Codex往返；若同一失败两次无新证据，停止重复并改变诊断方法。进入最后整体验收前核对剩余预算是否足够。完成/真实阻塞回到当前 hub，不要求用户切窗口。
