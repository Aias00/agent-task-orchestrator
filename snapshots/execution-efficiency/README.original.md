# Codex Task Runner

本地、串行、可恢复的 Codex CLI 依赖调度器。Python 3.12 标准库实现，无额外运行依赖，不使用 goal。由当前对话完成需求访谈与任务规划，后台进程完成执行、独立验收和有限重试。

执行时长可设为 `advisory`：按实测估时、记录耗时和超估预警，但不因时间额度耗尽停工或反复要求追加分钟数；同一本机策略同时覆盖旧任务与新任务，权限、人工停止、最终验收和重复失败保护保留。也支持显式选择有界弹性额度；命令、账本与迁移见 [预算机制](docs/adaptive-budgets.md)。

实现包含完整目标与阶段依赖、有限异常接管、统一用户入口、持久化主控待办，以及 Codex / Claude Code / Pi CLI worker 适配。架构、状态流与实现边界见 [设计方案](docs/architecture.md)，验证方法和已知限制见 [验证说明](docs/validation.md)。

通用会话消息层使用 SQLite 持久化消息箱和 Codex/Claude Code/Pi 受管适配器，见
[消息协议](docs/message-protocol.md) 与 [适配器接口](docs/agent-transports.md)。
源码已接入 interaction/control/recovery/delegation，支持显式注册的非 Codex origin 与 hub。受管通信会话保留原生上下文，旧 JSON、Codex UUID、授权与累计预算保留。本 checkout 不替换正在运行的部署；完整验收状态见 [交付验证](docs/message-delivery-validation.md)。

隔离候选新增无需 Codex 服务的本地 CLI hub/origin：私有角色凭证、实际用户答复来源、canonical 问答与前台续办见 [中立 CLI 操作说明](docs/session-decoupling/neutral-cli.md)。此路径不修改当前全局 hub，不把模型轮次结束当成工作流验收。

会话解耦前驱保留原 Codex UUID/API 兼容层，并提供独立的 `pi_acp` 托管协调会话；`pi` / `acp_pi` worker 与协调端能力分别判断，见 [前驱架构](docs/session-decoupling/architecture.md) 和 [能力表](docs/session-decoupling/capabilities.md)。前驱已由 origin 完成包括实际 Pi 1.5.2＋脚本化本地模型的九项验收，未部署；历史说明和原包保留，但其证明只对应不变的前驱。本 checkout 的后续 Linger 候选使用独立清单与门禁，见 [当前交付](docs/linger-continuation/operator-handoff.md)。本任务不重跑实际 Pi、不调用云端模型、不激活全局运行版。

## 安装与接入

需要 Python 3.12+、Git，以及已经配置可用模型的 Codex CLI。当前完整测试环境为 macOS；Claude Code/Pi worker 还要求系统提供 `sandbox-exec`。核心运行时只使用 Python 标准库。

```sh
git clone https://github.com/Aias00/codex-task-runner.git
cd codex-task-runner
python3.12 scripts/install.py
# 可选：同时安装对话 skill；已有同名入口/skill 时先检查，再显式使用 --force
python3.12 scripts/install.py --skills-dir "$HOME/.codex/skills" --force
ctask --help
```

安装器只写命令入口和显式选择的 skill，不启动服务，不迁移账号、知识库或会话。请将 `~/.local/bin` 加入 PATH。也可以直接运行 `python3.12 runner.py` 或 `./bin/ctask`；后者通过 `CTASK_PYTHON` 选择 Python，默认 `python3.12`。安装后的入口引用当前 checkout，因此移动或删除 checkout 后需重新安装。

在需要集中交互的 Codex 会话中核实真实 `CODEX_THREAD_ID` 后，按用户授权配置 `ctask hub` 和 `ctask control start`。长任务按下文建立完整 workflow，记录 origin 并启用监督。`supervise` 在 controller 已启用时登记观察；已有问题也会被 controller 扫描登记。仅安装 CLI/skill 不会把所有旧会话自动变成后台任务。首次配置和各类状态查询不应混同于恢复或新的业务授权。

仓库只保存可复用源码、文档和测试。业务 `.codex/tasks/`、本机 hub/controller 状态、模型认证及共享记忆正文各自保留在目标机，不随源码提交。

## 总任务与阶段

长期工作用 `workflow-create` 创建总任务。`create` 创建的单次 run 只表示一个阶段；它的 `completed` 不能代表整个用户目标完成。

```sh
ctask workflow-validate contract.json
ctask workflow-create --repo /absolute/path/to/repo --plan contract.json
# 已核实原会话 UUID 时，创建命令可加 --origin-thread UUID
ctask start /absolute/path/to/repo/.codex/tasks/workflows/WORKFLOW_ID
ctask status /absolute/path/to/repo/.codex/tasks/workflows/WORKFLOW_ID
ctask stop /absolute/path/to/repo/.codex/tasks/workflows/WORKFLOW_ID
ctask resume /absolute/path/to/repo/.codex/tasks/workflows/WORKFLOW_ID
```

总任务合约保存完整目标、范围、授权来源和最终验收条件，示例（将检查替换为工程的真实检查）：

```json
{
  "objective": "完成登录功能及其回归验收",
  "scope": "实现登录、错误处理和会话过期，保留已有接口约定",
  "authorization": "用户已同意本地实现和测试；不提交、推送、部署或发送消息",
  "acceptance": [
    {"id": "login", "description": "正确凭据可登录，错误凭据明确拒绝",
     "checks": [["python3", "-m", "unittest", "tests.test_login"]]},
    {"id": "expiration", "description": "会话过期后原令牌不可继续使用",
     "checks": [["python3", "-m", "unittest", "tests.test_expiration"]]}
  ],
  "memory": true
}
```

- 总体验收先执行一次，并在每个阶段完成后全部重查；后续阶段破坏已完成条件会重新进入待处理。
- 可选 `stages` 为预先确认的阶段数组，每项含 `id`、`covers`（验收 ID 列表）、`plan`（下文原有 run 格式）。已覆盖条件仍通过时可跳过该阶段。
- 未完成项没有待执行阶段时，只读 Codex 规划器根据完整合约、失败证据和已完成阶段生成下一阶段。生成任务必须引用已有验收 ID；程序使用合约中的原检查 argv，不执行规划器临时提供的任意验收命令。
- 合约与每个阶段计划分别固化并校验哈希；不能通过缩小目标、删验收或调低检查来获得完成。超出原范围的工作返回明确阻塞。合约改变需要重新整理并创建新总任务，不修改旧合约。
- 只有所有最终条件通过才标 `completed`。`needs_input` 表示人工验收未完成；`waiting` 表示有具体外部条件，有限等待后再检查；`blocked` 表示需处理前置条件；`stage_limit`、`planning_limit`、`limit` 表示预算到达边界，均不是完成。
- 外部权限与业务决策不由规划器放行。worker 仍采用既有 sandbox 和禁止自动提交/推送/部署的边界；确需宿主环境的验收必须事先写进已授权合约。宿主检查会执行命令，宜使用只读验收；合约不能作为不可信输入直接运行。
- 每个总任务的合约默认时长参考为 2 小时；`budget-policy` 的 `advisory` 策略使其仅用于估时，不是停工点。12 阶段、12 次规划、每次外部等待最多 300 秒；阶段沿用 3 次尝试、单次 worker 1800 秒、检查 120 秒。可在合约 `limits` 中设置各项参数；恢复保留累计耗时，单次卡死和无进展重试保护独立于总时长策略。
- `stop` 作用于总任务和当前阶段；恢复会处理未完成阶段并重新核对全部条件。存在未清理的子进程组时拒绝直接恢复。仓库锁覆盖总任务的所有阶段与规划，避免两个执行器并行修改同一仓库。

人工验收写为 `{"id":"visual","description":"用户确认界面满足约定","kind":"manual"}`。只有实际收到用户验收结论后，协调会话才记录证据并运行：

```sh
ctask acknowledge WORKFLOW_DIR --criterion visual --evidence /absolute/path/to/acceptance-record.md --note "用户已确认的具体结论与来源"
ctask resume WORKFLOW_DIR
```

证据内容、合约或后续阶段执行发生变化时旧人工验收失效；不得用本命令伪造用户确认。后台无法自行完成的人工条件明确停在 `needs_input`。

总任务记录在 `.codex/tasks/workflows/<ID>/`：`contract.json` 是不变的总约定，`state.json` 记录完整验收、剩余工作、累计预算和所有阶段路径。阶段中的 `parent.json` 反向关联总任务；`origin.json` 记录原会话来源。`ctask list` 同时列出两层，`status` 返回 `completion_scope=overall/stage_only`。旧 run 保持原语义，不会自动猜测其完整目标或合并成总任务。

## 使用

默认安装命令：`~/.local/bin/ctask`。

```sh
ctask validate plan.json
ctask create --repo /absolute/path/to/repo --plan plan.json
# create 返回以下命令需要的绝对运行目录
ctask start /absolute/path/to/repo/.codex/tasks/RUN_ID
ctask status /absolute/path/to/repo/.codex/tasks/RUN_ID
ctask stop /absolute/path/to/repo/.codex/tasks/RUN_ID
ctask resume /absolute/path/to/repo/.codex/tasks/RUN_ID
# 修复了阻塞条件或需要重新尝试失败任务时：
ctask resume /absolute/path/to/repo/.codex/tasks/RUN_ID --retry-failed
ctask list --repo /absolute/path/to/repo
```

`start/resume` 启动脱离终端的进程，命令立即返回；终端关闭后可继续。机器必须开机且未休眠；没有安装开机自动运行服务。停止后保存状态，通过 resume 继续。不要同时在同一工作区运行其他修改代码的 agent。

## 计划格式

```json
{
  "objective": "实现并验证某功能",
  "memory": true,
  "sandbox": "workspace-write",
  "limits": {
    "runtime_seconds": 7200,
    "attempt_seconds": 1800,
    "check_seconds": 120,
    "max_attempts": 3
  },
  "tasks": [
    {
      "id": "implementation",
      "prompt": "实现约定功能；具体验收见已有需求文档。",
      "depends_on": [],
      "checks": [["python3", "-m", "unittest", "discover", "-s", "tests"]]
    }
  ]
}
```

每个任务必须有独立检查命令，使用 argv 数组，默认不经过 shell。需要 shell 语义时显式使用 `["/bin/sh", "-c", "..."]`。检查本身也会执行代码，必须属于任务授权范围。纯人工验收不能伪装成 `true` 命令，应作为单独的待人工交付项。

默认上限可在每个计划中调整。可选 `model` 指定 worker 模型，否则沿用 Codex 配置。旧计划默认 workspace-write、approval_policy=never；新任务可显式配置 Codex worker 网络、额外写目录、能力前检及带来源的完整权限模式，不自动扩大旧任务权限。正常宿主交接与异常修复次数分开，详见 [worker 权限与宿主交接](docs/worker-permissions-and-handoffs.md)。执行授权边界写入 worker 提示，不等于对恶意 worker 或恶意仓库的安全隔离。

## 执行语义

- 验证依赖图，拒绝循环/缺失依赖；串行执行，仓库锁阻止本工具同时修改同一仓库。
- worker 每轮新建 Codex 上下文，注入任务、前置结果、失败反馈、共享记忆；实际代码和检查点保持在磁盘。
- worker 输出 complete 且所有验收命令通过才完成；continue 或检查失败会在上限内继续。
- blocked 保留具体原因，failed 表示尝试耗尽；其他独立分支继续。依赖这些任务的分支标 dependency_blocked。
- waiting 按有限等待时间唤醒，等待期间可推进独立任务；唤醒也计入尝试次数。
- 所有任务完成后再整体复查；失败任务及下游返回待执行，在上限内自动修复。
- resume 重新验收已完成项，失败项及其下游失效。`--retry-failed` 重置未完成任务的尝试次数。
- 固化的 plan.json 改动会中止执行。发现需要新增依赖时，先停止并形成修订计划，创建新 run；第一版不自动修改任务图。
- stop 写入 STOP 文件，worker 终止当前子进程组。硬杀 worker 后若仍有孤儿进程组，resume 会拒绝重复启动，必须先检查处理；不会仅凭旧 PID 盲目杀进程。

## 状态与记忆

运行记录在目标 Git 根目录 `.codex/tasks/<RUN_ID>/`：

- plan.json：固化任务与验收；state.json：原子更新的任务状态。
- events.jsonl：事件；worker.log：后台进程日志。
- evidence/：CLI 与检查输出；checkpoints/：每轮提示和结构化结果。

状态不是业务代码，提交前排除 `.codex/tasks/`。工具不会自动修改项目的 .gitignore、创建提交或推送。

当 `memory=true` 且 `~/.local/bin/bm` 存在时，运行前确定性读取偏好并检索相关知识，终结时写入带本地证据路径的运行摘要。已验证的技术经验仍由对话/worker 按共享记忆规则提取；调度器不会把模型的推测自动写成通用事实。停止/超时仍保留本地证据，记忆不可用不妨碍代码任务推进。

## 验证

```sh
python3.12 -m unittest discover -s tests -v
```

自动化测试用可控 fake CLI 注入失败、阻塞、等待、返修与停止，覆盖阶段成功但整体未完成、自动规划下一阶段、总体验收回归、人工确认、合约完整性、重复恢复和异常清理状态。真实 Codex 冒烟记录另外保存。当前实现没有网页面板、开机自启、多 worker 并发；自动接管需按下节显式登记原会话与授权来源；总任务通过本机协调进程持续推进，不依赖原聊天窗口保持活动。


## 阻塞后自动接管

监督进程独立于 worker，已启用时每 5 秒检查一次总任务。总任务发生 blocked/error/cleanup_blocked 或执行器意外退出时，持久化一次请求，通过持久化消息箱及已注册适配器通知已核实的原会话。不会新建业务目标、改变合约、扩大操作权限或直接修改业务代码。

新总任务由协调会话在已有持续推进授权下启用：

```sh
ctask workflow-create --repo REPO --plan CONTRACT.json --origin-thread UUID --auto-recover --recovery-source '用户确认持续推进并启用自动接管的来源'
ctask start WORKFLOW_DIR
```

既有总任务不改固化合约，用下面的命令接入（会开始监听，若已阻塞会发送一次请求）：

```sh
ctask supervise WORKFLOW_DIR --source '用户本次授权及来源'
ctask status WORKFLOW_DIR
ctask unsupervise WORKFLOW_DIR
```

`supervise` 默认最多 3 次修复接管、相同阻塞最多 2 次、单次接管响应/处理期限 30 分钟、监督窗口 4 小时；可用 `--max-requests`、`--same-blocker-limit`、`--response-seconds`、`--window-seconds` 设置首次登记值。达到上限仅增加一次 report-only 通知，请原会话说明情况并向用户提出必要决定；不能自行重置预算。再次 supervise 保留原请求、期限、计数和限制。监督窗口限制自动接管，不杀死原会话；修复时长由独立窗口约束，原 workflow 累计执行预算仍原样保留。

接管超时/修复证据失效（`stalled`）、原会话报告 `blocked`、`send_failed` / `delivery_unknown` 和 `resume_failed` 时，监督在退出前直接将异常写入主控 `inbox` 并通知已配置 hub。记录类型为 `supervision_alert`，每个接管请求只通知一次，投递不明不重发。`status` 的 `recovery.latest.notification` 展示告警 ID、投递状态或路由错误；未配置 hub 明确标记未路由。超时不会自动续期、重启、增加预算，也不证明原会话已停止工作。

主控根据真实状态和现有授权处理告警；需要用户决定时再询问。实际回复仍走 `answer/accept-answer`，新的续办指令不冒充旧请求回执。恢复、STOP、禁用监督、合约/会话变化后旧告警失效。后台监督即使继承了 hub 的会话环境变量也会真实入队，避免把后台异常误认为已在当前用户轮次呈现。该机制不是开机自启服务；监督进程被系统强制终止时仍需检查本机进程。

原会话收到请求后必须先运行消息内的 `takeover --state working`，请求过期、已停止、已恢复、绑定改变或有活动执行器时拒绝接手。修复并记录实际证据后：

```sh
ctask takeover WORKFLOW_DIR --request REQUEST_ID --state repaired --evidence /absolute/path/repair.md --note '实际修复与验证'
```

监督进程核对证据哈希后自动执行原总任务 `resume --retry-failed`，最终条件不变。恢复命令返回 launching 只标 resuming；只有实际 workflow_started/执行代次变化才记 resumed。原会话自行恢复也能被观察。成功后继续跟踪后续阶段，新的阻塞进入下一次有限接管。

需要用户决定/权限/凭据/人工验收时用 `takeover --state needs_input --note '具体缺口' --recommendation '推荐方案'`；已配置 hub 时自动送到统一入口，未配置才在原会话提问。无法继续且已明确原因用 `--state blocked`。它们不会触发重复排查。总任务 needs_input 或运行预算/阶段/规划上限只发送报告请求，不能登记 repaired，更不能伪造人工确认。

`recovery.json` 保存授权来源、目标会话、全部请求与去重记录；`supervisor.json` 保存心跳/进程；`supervisor.log` 保留监督异常。`status`/`list` 同时显示 workflow 与 recovery 状态。queued 是已入队，responded_at 是观察到该请求后的回复，working 是接手登记，repaired 是修复证据登记，resumed 是实际恢复，completed 才是总任务验收完成。

发送前先落盘 outbox；发送超时、崩溃或回执不明时标 delivery_unknown，不盲目重发。长时间无有效接管/恢复会标 stalled；启动失败标 resume_failed；这时需核对原会话和队列，而非另起平行修复。重复 supervise 不绕过去重。`stop` 的 STOP 文件阻止后续通知与接管，`unsupervise` 只关闭监督、不停止业务 worker；已经入队的消息无法撤回，原会话必须先通过 takeover 有效性检查。当前仍依赖本机开机、未休眠、Codex 会话服务可达；没有开机自启或跨机器通知。


## 统一用户交互入口

用户可以指定一个本机会话接收所有已接入工作流的提问，各任务继续由自己的 origin 原会话执行。核实协调窗口的 CODEX_THREAD_ID 后执行：

```sh
ctask hub --thread VERIFIED_HUB_UUID --source '用户要求全部在此会话交互的来源'
ctask hub
```

绑定存于 ATO_HOME/ctask-interaction/hub.json；未设置 ATO_HOME 时沿用 CODEX_HOME 下的旧位置。指定不同 ATO_HOME 不会自动迁移旧状态。ATO_HOME 仅定位 ATO 状态，CODEX_HOME 继续定位 Codex 原生索引和 rollout；两者都不是会话授权。新旧本机工作流都使用此默认入口，origin 不变；未配置时保留原会话提问。Codex 会话须在当前本机存储精确核实；受管 Claude/Pi 使用显式注册的 endpoint ID 和 host capability。不能从最新会话或迁移笔记猜测。

执行会话发起问题：

```sh
ctask ask WORKFLOW_DIR --question '需要用户决定的具体事项' --recommendation '推荐方案和原因' --context '必要背景'
```

自动接管使用 `takeover --state needs_input --note '具体问题' --recommendation '推荐方案'` 时会自动转送问题。发送结果保存在 ATO_HOME/ctask-interaction/questions/（未设置 ATO_HOME 时沿用 CODEX_HOME 的旧位置），包含 workflow、合约哈希、原会话、入口、问题 ID 与两次投递回执；不复制完整日志或凭据。

统一入口收到通知后核实问题仍有效、向用户提问，实际收到答复后转交：

```sh
ctask inbox
ctask answer QUESTION_ID --answer '用户实际答复' --source '此会话用户原文及时间来源'
```

原会话收到队列消息后先运行 `ctask accept-answer QUESTION_ID`，再根据实际答复解决输入并恢复原总任务；需要改范围时仍创建新合约，不能修改旧合约。代码不会把文本答复自动当作权限或人工验收，也不会替用户调用 acknowledge。已关闭的 needs_input 请求无需再 takeover working，正常恢复会重新启动已配置的监督，累计预算不变。

通知采用至多发送一次的持久化 outbox；发送回执未知不会自动重发。停止、完成、合约/来源变化或原阻塞已解决时拒绝迟到答复。命令核对当前 CODEX_THREAD_ID 或受管 host capability，问题只能由原会话提出、答复只能由该问题的入口发送、接收只能由原会话确认。它是协作校验，不是对恶意本机进程的权限隔离。默认入口改变不重绑已发问题。同一会话兼任两种角色时标 local，不向自身队列发送消息。当前迁移包不包含入口绑定、问题、答复或原会话存储，MacBook需单独设置自己的入口。


## 主控待办、结果回传与进度观察

业务 supervisor 的有限修复额度与主控跟进是两个独立机制。用户要求集中管理并持续跟进时，在已核实的 hub 会话启动一次：

```sh
ctask control start --source '本次用户明确要求持续跟进的原文/来源'
ctask control status
ctask followups
ctask presented QUESTION_OR_ACTION_ID --source '已在本会话向用户说明的实际位置/时间'
ctask control stop
```

controller 是独立本机进程，每5秒确定性读取状态和执行端流，不调用模型。SQLite/WAL保存在 `ATO_HOME/ctask-interaction/control/state.sqlite`（未设置 ATO_HOME 时沿用 CODEX_HOME 的旧位置），同一事务记录待办投影和事件；消息先写唯一 outbox claim 再发送。多进程共用锁和唯一键。未知投递/发送中崩溃保留不确定性，不自动重发或改一个ID重试。进程日志、PID、心跳在同目录。默认15分钟提醒一次，每个待办最多额外3次；达到提醒上限仍保留未解决状态。`control start` 可显式调整 `--reminder-seconds`（至少60秒）与 `--max-reminders`。定时检查不耗模型token，通知唤醒会话后的诊断/回复会消耗。

`followups`只读取，不自动标记已说明。问题投递、实际向用户说明、用户答复、原会话接收和真实执行分别记录；accepted不是恢复成功。真实workflow启动事件或执行代次推进只关闭该次恢复待办，不能证明总任务完成。已登记workflow继续被观察，即使原业务supervisor因额度/等待输入退出。新的问题、阻塞及最终完成结果会形成待办；完成结果要实际向用户报告后用`presented`登记。STOP/失效记录停止提醒，不称为完成。读取/解析失败保持可见异常，不按健康处理。

新答复投递记录原会话rollout起始偏移，controller根据精确问题ID和轮次关联原会话最终回复，并至多回传一次。历史答复没有偏移时不猜最后回复、也不全量读历史来凑关联；原有消息回执继续保留。同一hub/origin不自我回传。自动转回的是工作材料，不是新的用户授权；主控仍核实实际状态。切换hub需要先停止controller并确认退出，不重绑旧问题/待办。

预算追加若采用关联续办合约，原会话或hub显式登记，不能通过目录名猜测继承关系：

```sh
ctask followup-link QUESTION_ID --workflow NEW_WORKFLOW_DIR \
  --note '原会话已核实并创建的预算承接' --evidence /absolute/lineage.json
```

入口会核对原会话、工程、完整目标和全部验收相同。scope如附加承接说明，还要求保留旧scope前缀及结构化lineage，字段包括 `predecessor_workflow`、`successor_workflow`、`accepted_question` 和 `permission_expanded: false`。此命令只链接观察记录，不追加预算、不覆盖旧合约、不暂停/启动业务。前任记录保留，主控追踪新任务，避免旧blocked制造重复告警。

watchdog只观察实际模型worker，静默的构建/验收命令不会被当作模型卡死。统一归一化Codex JSON、Claude stream-json和Pi JSON中的工具开始/结束/进度；同命令内6次相同工具参数、120秒无输出、600秒工具无进展各去重告警。参数只保存hash。它不自动取消、修改业务、追加预算或接管正在写工程的进程；原有超时、修复次数、总预算与授权边界继续生效。主控根据新证据决定是否通知原会话诊断，不能仅凭告警认定失败。

后台进程依赖本机保持运行；没有新增开机自启。重启后在绑定hub重新`control start`读取已保存状态，不能声称关机/休眠期间仍在计算。业务恢复仍只由原会话和已有执行器负责。

## 新建独立协调会话并派发工程任务

工程路径与执行会话分开指定。只给路径默认当前对话负责（复杂任务仍可后台workflow）；指定已有会话则核实后续办；用户明确要求“在此路径新开独立执行会话”才使用 delegate。它创建的是持久化的原协调会话，不是某次阶段worker。

先在主控会话确认完整目标、范围、授权与最终验收，准备既有workflow格式的合约；目标须为已存在的Git工程，未初始化的工程由用户明确的初始化任务先行完成，工具不隐式git init。hub 必须已核实绑定当前主控：

```sh
ctask delegate --repo /absolute/project --plan /absolute/contract.json --name '工程任务名称' --source '用户明确要求新建独立执行会话的原文与来源'
ctask delegation-status /absolute/project/.codex/tasks/delegations/DELEGATION_ID
```

默认派发ID从工程、完整合约、名称、来源和hub推导；同一命令重试返回原记录。可用 `--request-id` 指定稳定唯一ID，不能用新ID绕过不明结果。默认 `--timeout 600` 是接受/启动窗口，不改变 workflow 的累计运行预算。新任务需不同ID/来源，已有未完成总任务或待核实派发时拒绝另起并行任务，可复用原任务或隔离worktree。

内部使用本机Codex daemon的Unix WebSocket JSON-RPC，thread/start创建非ephemeral线程，thread/name/set命名，thread/read及本地元数据核实持久化、cwd和请求的sandbox/approval。默认继承合约 workspace-write/read-only；额外写入根仅包含本工程 .codex/tasks 与本机hub状态目录。不会自动使用danger-full-access、调整业务授权、重启daemon或响应服务端审批。stdio proxy只是裸字节代理，不能向WebSocket控制套接字直接发送JSON行。

派发器建立 origin 指向新会话的workflow及监督，然后把接受请求入队。新会话读取约定后执行 `ctask delegation-accept DELEGATION_DIR`；本机dispatcher观察到接受才启动该workflow，新会话不能重复start/create。它随后保留完整上下文，负责后续阻塞接管；用户问题继续送hub。创建/消息回执/接受/实际workflow_started分别记为 thread_created、queued、accepted、started；started并不代表最终验收通过。

工程 `.codex/tasks/delegations/ID/` 保存请求指纹、完整合约、来源、会话ID、工作目录核验、回执和dispatcher日志。创建或发送结果未知会保存 creation_unknown/delivery_unknown，重复请求不会自动再创建/再发消息；已送达的未知消息可通过原会话真实accept消除启动歧义。setup_failed/start_failed/expired需要检查记录，不自动扩大权限或重置期限。STOP与合约改变阻止迟到启动。此入口需要本机daemon可达，与现有监督一样没有开机自启；跨机器要单独配置本机hub。

## CLI 执行端：Claude Code 与 Pi

`ctask agent-doctor` 检查CLI版本和认证准备情况。新plan/workflow指定 `"agent": {"kind": "claude", "model": "可用模型ID"}` 或 `"agent": {"kind": "pi", "model": "provider/model-id"}`。model可省略使用原生默认；第三方模型写agent.model，不传顶层Codex model。默认仍Codex，旧固化任务不自动切换。workflow只读规划器和生成阶段均继承agent；原最终验收由宿主独立执行。

安装验证版本：Claude Code 2.1.325；Pi `@earendil-works/pi-coding-agent` 1.5.2。工具装在 `~/.local/share/ctask-agent-clis`，不写业务工程。查找顺序为CTASK_CLAUDE_BIN/CTASK_PI_BIN、PATH、该目录node_modules/.bin。二进制覆盖必须来自受信任本机配置，计划不能传任意CLI flags。认证使用原生CLI登录/配置或环境变量，不把key放入plan、argv、日志或共享记忆。上游旧@mariozechner包已迁移，不保留两个pi入口。

第一版仅支持macOS外部worker，要求sandbox-exec，不提供无沙箱回退。模型仅有文件读/查找/编辑/写入工具；无Bash、Web、MCP、子agent或自动加载的skills。构建/测试通过既定宿主checks执行。Claude启用safe-mode/restricted/dontAsk及明确工具清单；Pi关闭自动扩展/MCP/skills/上下文资源，只显式加载pi-path-guard.mjs。guard按真实路径检查范围和外部符号链接；模型不能写.git、.codex或原生认证目录。OS策略限制文件写入，允许受信任CLI自身认证/缓存目录和本次runtime；CLI客户端仍需联网调用模型，这不是Codex原生沙箱的完整复制。只读规划器无写工具。

原生输出、stderr、调用信息和session/usage元数据在checkpoints/<attempt>.agent-runtime，目录0700。只接受成功终态的完整JSON，partial、错误、工具输出或截断不能冒充完成；缺认证返回blocked，不反复尝试。STOP/超时按同一进程组管理bridge、CLI及子进程，仓库锁/累计预算/独立验收保持原机制。

上述 agents.py 阶段 worker/只读 planner 每次按完整合约和检查点重启；Claude worker 不持久化会话，Pi worker 保留本次 session。通信协调端使用另一套 message_adapters.py：Claude stream-json/精确 resume、Pi RPC/精确 session，支持多轮上下文和显式受管 origin/hub。第三方使用 origin_endpoint_id，不覆盖旧 origin_thread_id。协调端默认无工具，由可信 host 执行有权限的状态转换；模型结构化提问/接收事件不授予 shell、额外预算或业务执行许可。不支持注入任意既有交互窗口。

验证分别报告：fixture CLI协议/进程测试，实际安装Pi+本地模拟模型端点（真实路径guard、write工具及独立验收），以及云端模型调用。前两者通过不证明账号额度或云端模型效果。缺少Claude登录或Pi provider凭据时是认证阻塞；真实云端验收需在目标机独立配置。开发环境另外验证过Pi真实provider调用和两阶段workflow，不能推广为每台机器已有登录或任意模型都同样可靠。仓库不携带node_modules、认证或会话；目标机独立安装CLI。非macOS暂不执行第三方worker。

官方依据：[Claude headless](https://code.claude.com/docs/en/headless)、[Pi CLI](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/cli.md)、[Pi JSON](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/json.md)。已按安装版本--help核对，升级需复验。

持久化消息适配器同一受管会话串行接收后续消息。协议与混合 origin/hub 使用独立进程 fixture 验证；真实 Claude 2.1.325/Pi 1.5.2 曾由外部宿主通过本地 provider 双轮验证，后续源码变更须重新原生验收。worker 的 socket/nested sandbox 限制不等于宿主失败，也不能用旧证据宣称当前完整目标通过。未验证云端账号。接口与证据见 [agent-transports](docs/agent-transports.md) 和 [交付验证](docs/message-delivery-validation.md)。


### Agent-neutral workflow routing (feature checkout)

`interaction`, `control`, `recovery` and `delegation` deliver through the shared
persistent mailbox and transport adapters. Managed Claude/Pi origins use explicit
`mailbox-register` and `workflow-create --origin-endpoint`; existing Codex UUIDs,
JSON outboxes, authorization sources and accumulated limits remain valid.
Managed models can emit structured questions; actual sourced user answers return
through the existing hub and origin-acceptance flow. A foreground `mailbox-pump`
processes one managed native turn with tools disabled. No installed runtime is
activated by this checkout. See [managed host interfaces](docs/agent-transports.md)
and [workflow guards and compatibility](docs/message-protocol.md).


### Inactive continuation accounting candidate

New plans may explicitly select `execution_policy.version=2` to separate normal
continuations, external waits and cooperative yields from genuine failures while
retaining cumulative invocation history. Absent-policy plans keep their existing
behavior. See the [accounting stage checkpoint](docs/linger-continuation/accounting-checkpoint.md)
for the exact policy, progress evidence, finite guards and original host gate.
The additive read-only `status.task_view` and `task-summary DIRECTORY --format
json|text` show full objective versus stage, recorded and observed status,
verified process identity, remaining checks, distinct counters and recovery.
Legacy failures remain unknown. Queries do not start work, send notifications,
mark presentation or accept anything. See the [presentation stage checkpoint](docs/linger-continuation/presentation-checkpoint.md)
for examples and validation. New plans may separately opt into bounded endpoint
admission with `capacity: {"endpoint":"local-alias","priority":"background"}`
(or `interactive`). Only trusted `ATO_HOME/capacity.json` sets the global limit
and interactive reserve; plans cannot override them. Runner workers and Workflow
planners/stages queue durably before invocation. STOP cancels pending requests;
claimed slots require exact receipts and confirmed cleanup, with no TTL reclaim
or preemption. Existing tasks acquire no policy or capacity changes automatically.
Queue age and possible background starvation are visible in the same read-only
task summaries. See the [capacity stage checkpoint](docs/linger-continuation/capacity-checkpoint.md)
for configuration, integration, restart behavior and exact host handoff.
The complete inactive source bundle is `build/linger-continuation/candidate.tar.gz`,
with portable SHA256/modes and frozen baseline/predecessor identities in
`build/linger-continuation/candidate.json`. Verify existing bytes without installing:

```sh
python3.12 scripts/prepare_linger_candidate.py --check
```

Preparation (`python3.12 scripts/prepare_linger_candidate.py`) always writes a
deterministic archive and preserves the predecessor bundle. It includes no
task/auth/dependency state. Original host qualification remains separate from
packaging and stage success. The exact final host commands, current evidence and
remaining acceptance are in [validation](docs/linger-continuation/validation.md)
and [operator handoff](docs/linger-continuation/operator-handoff.md); mechanism
details and rollback boundaries are in [architecture](docs/linger-continuation/architecture.md)
and [migration](docs/linger-continuation/migration.md). The retained actual-Pi
proof qualifies only the unchanged session-decoupling predecessor. This task
does not activate a runtime, migrate live state, publish Git or call real models/Pi.

### Offline upgrade and rollback

This source snapshot has no `scripts/upgrade_runtime.py` implementation. Use
only the delivered inactive packaging and extraction checks; live activation
and migration need separate authorization and an exact operator procedure.
The [execution-efficiency migration document](docs/execution-efficiency/migration.md)
describes future version selection and rollback boundaries. Never restore an
old controller/task database over new writes or rebind existing native IDs.

### Inactive execution-efficiency verification candidate

New plans may explicitly declare `verification_policy.version=1` with check
entries keyed by actual task/criterion ID, each containing only `inputs`, `env`
and boolean `cacheable`. The actual Runner and Workflow reuse only unchanged,
complete host-verified offline inputs with intact original evidence. Unknown
dependencies run the original argv uncached; legacy absent-policy behavior,
fresh final/full integration and manual/external acceptance remain enforced.
See [policy and trust scope](docs/execution-efficiency/incremental-verification.md)
and [stage evidence and original host argv](docs/execution-efficiency/verification-stage-handoff.md).
This is a local inactive candidate, not a deployment or full-task qualification.

### Integrated inactive execution-efficiency candidate

The [full execution-efficiency contract](docs/execution-efficiency/task.md)
integrates code-owned runtime selection, workflow responsibility/disposition,
exact task-bound native worker turns, host-validated incremental checkpoints,
complete offline check fingerprints and measured phase timing. Absent versioned
policies preserve legacy behavior. Warm sessions preserve all STOP, ownership,
project/contract/profile and unknown-delivery barriers. Native context reuse
still starts a CLI process per turn; unobserved model latency remains unknown.

Prepare `build/execution-efficiency/candidate.json` and deterministic
`candidate.tar.gz` with `python scripts/prepare_efficiency_candidate.py`; validate
with `--check`. A fresh extraction supports `--check-extracted` without private
host state. The bundle includes the frozen baseline and exact Linger677
predecessor manifest, with all protected tests unchanged, and includes neither
dependencies nor task/auth/native history. It activates nothing and does not
install the repository skill.

See [architecture](docs/execution-efficiency/architecture.md),
[migration and rollback](docs/execution-efficiency/migration.md),
[validation and limits](docs/execution-efficiency/validation.md) and
[exact operator handoff](docs/execution-efficiency/operator-handoff.md). Original
host regression/delivery and one origin-owned bounded actual Codex comparison
remain independent gates. `scripts/verify_execution_efficiency_local.py
--describe` is inert; its `--existing-profile` path is exclusively for that
explicit origin contract (<=12 turns/1200 seconds, no automatic resample).
Fixture counts never qualify a provider or establish speedup/full delivery.
