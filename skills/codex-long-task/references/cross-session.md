# 原会话关联与跨窗口续办

创建后台任务时读取“记录来源”；查询关联不明的旧任务、跨窗口续办或测试唤醒时再读后续流程。此文涵盖手工续办和已启用的自动监督；未登记 supervise 的旧任务不会自动通知。

## 记录来源

在 `ctask create` 返回的 `RUN_DIR` 中另存 `origin.json`，不要把额外字段写进已固化的 `plan.json`，也不要手改 `state.json`。记录：

使用 `workflow-create` 时程序已保存总任务 `origin.json`；核实会话 UUID 后通过 `--origin-thread` 提供，不覆盖已有来源。总任务自动创建的阶段通过 `parent.json` 关联 WORKFLOW_DIR；跨窗口续办优先定位总任务并查完整剩余条件，而不是只恢复某个阶段。

候选运行时还允许可信代码注册、验证过工程与身份的 `--origin-ref REF`，与旧 `--origin-thread` 互斥；旧字段及固化合约不被改写。内置 Claude/Pi 尚无原生会话注册、投递或创建实现，不能拿它们的临时 worker session_id 冒充来源。第三方适配器能力、迁移与实际验证范围见 `docs/agent-abstraction.md` 和 `docs/agent-abstraction-migration.md`。

- `run_id`、`repo`、`created_at`：实际任务编号、仓库绝对路径、带时区的记录时间。
- `origin_thread_id`：创建/协调该任务的原会话 UUID；不是后台 worker 的会话 ID，也不是任务编号。
- `codex_home`、`host`：会话所在本地 Codex 数据目录与机器标识，不保存凭据。
- `evidence`：如何核实关联，例如当前会话运行时提供的 ID，或会话记录中的实际 create/start 命令和返回目录。

只采用可核验的运行时信息；有多个候选不能猜测。没有可靠会话 ID 就写 `origin_thread_id: null` 并说明缺失原因。已有来源记录先读再合并，不用接手会话替换原会话；历史任务补录应标明核验日期和证据。

同时在 `RUN_DIR/handoff.md` 保存或链接已有执行约定：目标、范围、验收、用户明确决定与授权来源、未决问题、前置条件、工作目录及下一步。复用既有文档，避免复制完整聊天。阶段结论变化时由协调会话更新；共享知识库只保存可复用摘要，不承担实时任务状态。交接记录是证据，不产生新授权。

## 选择处理方式

- **查进度**：直接查任务状态与日志，无需唤醒原会话。
- **试试能否唤醒**：发一次只读诊断请求，让原会话复述任务约定并查现状；不把测试扩大为恢复、部署或数据修复。
- **让原会话继续**：在用户已经要求续办的范围内发送请求，携带任务目录和当前阻塞，请原会话核对历史约定后处理；不把“查询”解释为续办授权。
- **当前窗口接手**：先读执行约定、检查点和已有决定，核实哪些上下文可恢复。先查可自行找到的信息，只追问影响下一步的关键缺口，不重做整个访谈。

已获授权的同一续办请求不再重复确认。手工发送应有具体用户请求；已明确启用自动接管的 workflow 由 recovery.json 中的来源和限额约束持续通知。不要因为 skill 被自动加载或发现其他未登记任务 blocked 就扩大通知范围。

## 定位并发送

1. 优先读取 `origin.json`。缺失时在目标机器的 Codex 会话记录中按精确运行编号或目录检索，再核对创建/启动事件与会话元数据。仅提及该任务的旁观会话和 worker 会话不能作为来源证明。不使用 `--last` 猜测，不把所有历史会话正文灌入上下文。
2. 核对本机 `codex queue --help`。已实测 Codex CLI 0.165.0 支持 `codex queue --thread SESSION_UUID --message TEXT`；其他版本按实际帮助使用，不能假定有 `--resume`。
3. 发送前记录目标会话、请求唯一标识、发送时间和消息范围，并记下会话事件位置或当前轮次。消息保留用户本次约束及原任务授权边界；通过结构化参数或安全 argv 传递，避免把消息当作 shell 代码。
4. 同一请求只发送一次，保存返回的消息 ID。命令超时或返回不明时先检查是否已经入队，不能盲目重发。不要同时在当前窗口恢复/修改同一任务；原会话负责所请求的续办。

原会话可能经过上下文压缩，不承诺它拥有逐字完整历史。该机制要求会话存储和服务可访问；复制技能与知识笔记到 MacBook 不会复制 Mac mini 的会话或任务状态。

## 核对结果与失败处理

- 命令成功或返回 `Queued message` 只代表已入队。
- 目标会话出现与本次请求对应的新轮次（例如 `task_started`、`turn_trigger=queue`）及助手响应，才报告已唤醒。最好核对诊断回复复述的原约定，而非只看一句“收到”。
- 请求续办时，还要检查任务实际状态、日志或新运行目录；原会话响应不代表 worker 已恢复，后台恢复也不代表验收完成。
- 原会话创建替代阶段或独立验收 run 后，应沿 `parent.json` / 总任务阶段列表跟踪完整目标；旧 run 保持历史状态不能作为新工作未执行的证据，新阶段 completed 也不能替代总体验收。
- 轮询有界并保持进度说明；本轮无法等到响应时报告“已入队、尚未确认响应”，保留消息 ID 便于后续查询，不谎报成功或把等待当作许可。
- 原会话不可用时可研究 `codex exec resume SESSION_UUID` 或 app-server 恢复，但仅在确认无活动处理者、无待执行的重复请求且能够保持原请求权限边界后使用。不能在消息可能仍会送达时另起恢复进程；无法核实就保留队列状态并说明具体阻塞。
- SSH 被沙箱阻止应报告执行环境阻塞，不能推断成服务故障、凭据错误，也不能为续办自动放开权限或重复相同失败。

本机只读唤醒实测依据：共享记忆 `projects/long-task-workflow/cross-window-queue.md`，Codex CLI 0.165.0。尚未验证关闭客户端、daemon 停止或跨机器的唤醒。官方命令文档：https://learn.chatgpt.com/docs/developer-commands?surface=cli 。


## 自动接管协议

用户确认自动接管后，`supervise` 核实 origin 指向的本机原会话并保存来源，独立 supervisor 监听 workflow；它不自动推断其他会话，不把复制到 MacBook 的历史记忆当成会话存储。

收到 `[ctask-supervisor request=...]` 时先按消息执行 `ctask takeover WORKFLOW_DIR --request ID --state working --note "开始诊断"`。检查失败表示已停用、已停止、已恢复、请求过期或执行器冲突；不要绕过检查修改代码/重启，也不要创建另一个总任务掩盖旧请求。

核对原授权、现状与具体证据，解决可处理的根因。修复后记录真实验证文件，执行 `takeover ... --state repaired --evidence FILE --note "具体修复"`；监督进程校验证据后负责恢复同一 workflow。若采用直接 resume，必须核对实际启动，supervisor 会按运行事件确认。保留总体验收与累计预算，不凭 worker 自述批准人工验收。

无法自主解决时，登记 `needs_input` 并向用户提出具体缺口；明确不可继续则登记 `blocked` 并说明原因。report-only 请求只能报告/追问，不登记 repaired、不重置预算。不得将分析工具只读误解为否定原会话已授权业务交付，也不得从通知本身推导新的生产写入权限。

每次通知先持久化、至多发送一次；回执不明不会盲目重发。默认 3 次修复、同类阻塞 2 次，超限只再发送一次报告。响应/处理期限 30 分钟，监督窗口 4 小时；原任务预算独立保留。原会话和 supervisor 的状态分开报告，当前窗口不要并行处理其业务代码。

接管超时/证据失效、blocked、发送失败或结果未知、恢复失败时，监督在退出前直接向已配置 hub 写入并投递一条 `supervision_alert`。同一请求仅一条，重复检查或重启不重复发送；通知失败/未知保留在 `recovery.latest.notification` 和 `inbox`，不能称为已送达。未设置 hub 会明确记录未路由，不猜测其他会话。

告警不续期旧请求，不自动增加修复次数，不取消仍在运行的原会话。主控核对最新状态后按既有授权处理；确需用户决定时集中追问。实际答复经 `answer/accept-answer` 返回原会话；新的明确续办指令可用于核实证据后恢复同一总任务，但不得重置累计预算或冒用过期 takeover。STOP、禁用监督、合约/原会话变化、后续执行代次或新接管都会使旧告警无效。


## 问题与答复集中到用户入口

`ctask hub` 显示本机当前统一入口，`hub --thread UUID --source "来源"` 在用户要求后设置；绑定存于 CODEX_HOME/ctask-interaction/hub.json，不进入迁移包。任务 origin 仍指向原执行会话，不能为了统一交互把 origin 换成协调窗口。

原会话的普通问题用 `ctask ask WORKFLOW_DIR --question "问题" --recommendation "推荐" --context "背景"`。自动接管登记 needs_input 会直接转送 note 和 recommendation。每次问题有独立 ID，绑定 workflow、合约哈希、原会话与当时的交互入口。后续换默认入口不会悄悄改变已投递问题的归属。

入口会话收到通知，先 `ctask inbox` 核对状态再在本会话向用户提问；用户实际回答后 `ctask answer QUESTION_ID --answer "原答复" --source "本会话用户答复时间/原文来源"`，不得从转发通知或工作日志推断批准。答复经队列回到原会话，原会话必须 `ctask accept-answer QUESTION_ID` 后才处理，并核对实际授权。只记录接收并不等于已恢复；后续仍按 workflow 真正执行和最终验收判定。

问题/答复均采用持久化、至多发送一次；发送未知不自动重发，重复完全相同答复只返回记录。问题过期、STOP、已完成、合约或原会话变化会拒绝迟到答复；同一会话兼任入口和原会话时不自发队列消息。具体记录在 CODEX_HOME/ctask-interaction/questions/，不复制凭据或完整业务日志。


## 从路径创建新的独立协调会话

仅用户明确要求新独立会话时使用 `ctask delegate`，只有路径默认仍由当前会话负责。现有会话续办不走新建入口，也不能为了绕过旧任务阻塞另建目标。

派发器经本机 app-server Unix WebSocket 的 thread/start 创建并持久化会话，再 name/set 和 thread/read 核验；保留实际返回ID，检查 cwd、sandbox、approvalPolicy 和本地rollout，不查询“最新”推测目标。它用完整合约创建 workflow 并把 origin 设为新会话，hub 保持用户入口。新会话收到 ctask-delegation 后只读约定并 delegation-accept，本机派发器负责启动；当前轮不另起调度器、不并行修改业务代码。

派发记录位于工程 .codex/tasks/delegations/ID/，包含合约副本、来源、会话关联、消息回执和启动证据。重复同一请求至多创建一次和发送一次；超时/回执不明不盲目重复。dispatcher 等待真实accept后启动同一workflow；已有活动执行器或未完成总任务时拒绝另一派发，先使用原任务或隔离worktree。不会自动扩大沙箱、重启Codex daemon或批准服务端权限请求。

## 不向用户入口直接投递后台状态（2026-11-17）

当notification-policy为user_input_only时，原会话将进度、部分样本、普通完成等无需用户介入的结果写入原任务evidence；不得绕过controller直接用codex queue向hub发送状态。真正需要用户决定、授权、补充信息或人工验收时使用ctask ask/needs_input，保持canonical问题/代次/原会话检查。

源分发提供可选Codex投递入口guard（hub_queue_gate.py，须由用户明确安装后才生效）：仅对当前user_input_only hub的queue生效；普通消息元数据记为recorded_only，不生成Queued message，不投递。其它命令和其它目标仍exec原生程序，原生二进制未修改。不能将recorded_only误读为queued/accepted/恢复；不能用伪造question前缀绕过canonical校验。显式调用包内原生二进制或其它app-server客户端不经过该launcher，仍必须遵守禁止向hub投递普通状态的规则。
