---
name: codex-long-task
description: 自动识别需要多阶段实现、依赖调度、持续验证或等待恢复的已授权代码任务，交给本机后台执行器，可指定 Codex、Claude Code 或 Pi CLI 阶段执行端；用户无需说“长任务”或指定 skill。也用于后台任务状态查询、停止、恢复及跨窗口唤醒原会话续办。不用于普通问答、简单修改或已被调度器分派的 worker 子任务，不使用 goal。
---

# Codex 长任务执行

本机入口 `ctask`。完整计划格式与命令见 `CTASK_SOURCE/README.md`，首次创建计划前读取。

## 完整目标与阶段边界

- 需要持续推进的长任务默认使用 `workflow-create` 总任务入口。先从用户已确认的完整意图建立 `objective`、`scope`、`authorization` 和最终 `acceptance`；不能只拿最近一个报错、当前补丁或独立验收当作总目标。
- “继续”应回到现有总任务检查剩余条件，不要求用户反复重述完整目标。缺少总任务记录时先从原会话和交接资料重建；无法核实的最终范围只追问关键缺口，不把整个工程路线图擅自纳入授权。
- 每个阶段只覆盖明确的验收 ID。总任务协调进程会在阶段结束后重查全部条件，必要时规划下一阶段；检查 argv 固化，生成阶段不能替换最终标准。人工验收、外部等待和权限阻塞必须显式记录。
- `ctask create` 仍可用于单独阶段或复核，但 `completion_scope=stage_only` 不得汇报为整个工作完成。新建验收 run 必须关联原总任务或在交接中明确只是阶段，不用一个新 completed 掩盖其他未完成项。

## 工程路径与执行会话

- 只给工程路径或普通“在这里做某事”，默认当前对话负责；复杂任务仍可使用后台 workflow，origin 是当前会话。不要把后台 worker 当成新的原协调会话，不根据路径擅自复用历史会话。
- 用户明确“交给某已有会话”时核实那个会话再按跨窗口续办；明确“在路径新开独立执行会话”时才使用新会话派发入口。路径定位工程，不自行决定会话归属。
- 新会话派发先在当前会话明确完整目标、范围、授权和最终验收，核实目标 Git 工程、统一入口及既有执行器。然后 `ctask delegate --repo REPO --plan CONTRACT --name "任务会话名称" --source "用户明确要求独立会话的来源"`。可用稳定 `--request-id` 标记同一派发；重试同一请求不另建会话。非 Git/不存在的目录先按用户任务明确完成初始化，不隐式建立仓库。
- 派发器创建真实持久化协调会话，核实目录、身份、执行权限，建立 origin 指向它的 workflow、hub 关联和监督。它发一次接受请求；新会话先 `ctask delegation-accept DELEGATION_DIR`，派发器才启动那个总任务，新会话不能重复 start/create 或并行改代码。
- 用 `ctask delegation-status DELEGATION_DIR` 区分 thread_created、queued、accepted、started 和总任务验收状态；只创建/入队不能报告已开始执行。创建/发送结果未知时保留记录，不猜 UUID、不盲目另建或重发。默认接受窗口10分钟，原任务预算独立保留。
- 独立协调会话保留完整上下文，后续按 supervisor 接管；用户问题统一通过已绑定 hub。新会话不等于继承父会话所有历史或额外生产权限，传递的是完整任务合约及授权来源。

## 可选 CLI 执行端

- 用户指定Claude Code或Pi执行时先 `ctask agent-doctor` 检查安装/认证，新plan/contract显式设置 `agent.kind=claude|pi`，模型放 `agent.model`。默认仍Codex，不改既有固化任务。接口与限制见执行器README的“CLI 执行端”。
- 第一版第三方只承担受限文件工具worker和只读planner，构建测试由宿主原checks执行；不擅自开放shell/MCP/子agent。外部worker要求macOS sandbox-exec，不可用时报告阻塞。
- origin/hub仍是已核实Codex会话；第三方session_id仅作本次调用元数据，不能覆盖origin。恢复从完整合约、当前文件和前次反馈重启CLI，不声称接续其原生聊天历史或管理已有第三方交互窗口。
- 安装、fixture协议测试、真实CLI本地模拟端点与云端认证调用分别报告。缺登录/key先完成可验证工作，再让用户在本机原生CLI登录/配置，凭据不进入任务记录/共享记忆。

## 自动判断执行方式

- 根据实际工作量和执行结构判断，不依赖关键词，也不让用户选择“是否属于长任务”。需要多个有依赖的实现阶段、反复验收修复、外部等待或中断恢复时，主动采用调度器。
- 用户只需描述目标，例如“完成登录模块并把测试跑通”。先检查现状；若适合后台执行，简短告知采用的方式并按已有授权启动，不追加“是否启用长任务模式”的确认。
- 简单且能直接完成的修改在当前会话处理，不为流程本身拆任务。用户明确要求前台执行或逐步指导时遵从。
- 由调度器分派、要求返回结构化结果的 worker 已处于执行流程中，只完成分派任务，不再次启动 ctask 或重复需求访谈。

## 从对话交接

1. 保留用户已确认的目标、边界和默认项。需求尚有关键缺口时使用 grill-me；已确认事项不要重复访谈。
2. 在目标工程按现有文档约定整理需求、验收和任务依赖。每个任务用明确 prompt、depends_on 和实际有效的 checks（argv 数组）。模型负责提出相关异常验收，不能用空检查或永远成功的命令冒充验证。外部服务、网络、SSH 和写入路径应按实际 worker 环境核对；当前窗口可访问不代表 worker 沙箱也可访问。无法提前核验的条件作为明确前置检查，不反复重试已知环境阻塞。
3. 使用已经授权的计划直接推进。默认仅本地修改与验证，不自动提交、推送、部署或发消息。告知采用的可调整运行上限，不因常规参数反复请求确认。
4. 总任务执行 `ctask workflow-validate CONTRACT`、`ctask workflow-create --repo REPO --plan CONTRACT`；已核实原会话 UUID 时加 `--origin-thread UUID`。记录返回的 WORKFLOW_DIR，按 [原会话关联与跨窗口续办](references/cross-session.md) 补充紧凑交接，再用 `ctask start WORKFLOW_DIR` 启动。单独阶段才使用 `validate/create`；无法确定会话 ID 时记录缺失，不因此阻止已明确授权的任务。
5. 检查 `ctask status RUN_DIR` 和 worker.log，确认状态已进入 running 或完成；只得到 launching/PID 不能保证启动成功。不要在同一工作区另起修改代码的进程。
6. 向用户报告运行目录、当前状态和查询/停止方式。后台任务启动后可以结束当前回复；不要声称尚未验收的任务完成。机器休眠/关机期间不运行，没有开机自启。

## 查询、停止、恢复

- 查询：`ctask list --repo REPO` 与 `ctask status RUN_DIR`，需要时读取 evidence 和 events。
- 同时查看总任务与阶段，报告总目标、尚未通过的条件、当前阶段和阻塞。`observed_status` 与原始 `status` 不一致时以实际进程核验为准；不能仅看残留 running 或历史旧 run 的 blocked。一般“本机任务”还应区分调度器任务与其他 Codex 会话，不能把 ctask 清单当成全部未完成工作。
- 跨窗口查询可直接读取状态；访问任务文件不代表掌握原会话全部上下文。用户要求原会话继续、跨窗口接手或测试唤醒时，先读 [原会话关联与跨窗口续办](references/cross-session.md)，核对来源与请求范围后处理。
- 停止：`ctask stop RUN_DIR` 后检查任务确已停止；本地状态保留。
- 恢复：`ctask resume RUN_DIR` 会重新验收已完成项。缺失输入已解决或获授权重新尝试时加 `--retry-failed`。
- 总任务恢复仍受累计时间、阶段数和规划次数限制；不能自动重置预算来掩盖停滞。人工条件仅在用户实际验收后通过 `acknowledge` 记录带来源的证据，再恢复核验，不替用户批准。
- 新依赖/范围变化：停止当前 run，修订任务图并创建新 run；不要悄悄改固化的 plan.json。未完成分支的失败与人工决策须如实汇报，其他独立工作正常推进。
- 如果恢复提示孤儿进程，先核对实际进程身份和状态再处理，不按旧 PID 盲目终止。
- 汇报区分：消息已入队、原会话已响应、后台任务已恢复、任务验收通过。查询时给出本次实际状态与阻塞原因；worker 自述测试通过仍标明来源，不能据此宣称整体验收完成。

## 自动异常接管

- 用户已授权持续推进并启用本机自动接管时，创建总任务使用 `--origin-thread UUID --auto-recover --recovery-source "实际授权来源"`；原会话必须核实。既有任务用 `ctask supervise WORKFLOW_DIR --source "来源"` 接入，不改固化合约；没有来源不要凭共享记忆创造授权。
- 本次技能维护自身执行器时可以在隔离目录前台实现和验证，避免依赖待修改的执行器自举。
- 自动请求到达时读取 [跨窗口续办](references/cross-session.md) 的自动接管协议；先 `takeover --state working`，无效/过期请求不得修改或恢复。修复证据落盘后登记 `--state repaired --evidence FILE --note "验证"`，由监督进程恢复原总任务；不能只说修复完成而丢下总目标。
- `status` 中 recovery 单独表示通知/接手/恢复状态；queued 不等于响应，working/repaired 不等于 resumed，resumed 不等于整体完成。接管预算、重复失败及发送未知状态不得自动重置或绕过去重。
- 接管超时、原会话登记 blocked、发送失败/结果未知、恢复失败会直接产生 `supervision_alert` 并去重通知已配置 hub，不依赖原会话再次提问。查询同时查看 `recovery.latest.notification` 与 `inbox`；空的普通问题列表不代表任务健康。告警是状态材料，不是新的续办授权；需要时转交用户实际答复，原会话核实后处理。

## 统一用户交互入口

- 用户要求持续集中管理、或处理 `ctask-followup` / `ctask-origin-reply` 时，读取 [主控持续跟进](references/controller.md)。使用独立controller持久化跟进“已通知/已说明/已答复/原会话接收/实际恢复/结果已报告”，不依赖对话记忆；查询不算已说明，轮次结束不算整体完成。controller不扩大业务supervisor额度或原权限。

- 用户要求集中到指定会话交互时，核实该会话 ID 后使用 `ctask hub --thread UUID --source "用户指令来源"` 设定本机入口；保持各任务的 origin 不变。复制记忆/迁移包不复制会话绑定。
- 执行会话需要用户决定、授权、凭据说明或人工验收时先查 `ctask hub`。已配置时用 `ctask ask WORKFLOW_DIR --question "具体问题" --recommendation "推荐与原因" --context "必要背景"`，不要求用户切回执行会话。`takeover --state needs_input --note "问题" --recommendation "建议"` 会自动路由；路由失败查看返回结果，用同 request 重试登记，不重复接管或假称已通知。
- 统一入口收到 `[ctask-question]` 后用 `ctask inbox` 核实未过期，标明任务、问题和建议再向用户提问；不代答或并行修改业务代码。收到实际答复后用 `ctask answer QUESTION_ID --answer "实际答复" --source "用户答复来源"`；多个问题映射不清时先澄清，不能把一般的“好”扩大为批准全部任务。
- 原会话收到 `[ctask-user-answer]` 后先 `ctask accept-answer QUESTION_ID`；拒绝意味着已处理/已停止/合约或问题已变化，不执行迟到动作。核对用户答复与原授权后继续处理，解决输入后恢复同一总任务并核实真实运行；不要对已关闭的 needs_input 接管请求重复 working，不重置预算、不从转发消息推导额外权限。
- 入队、原会话确认接收、实际恢复是不同状态；`inbox` 记录问题及答复投递。发送回执不明不盲目重发，已发答案不覆盖。原执行会话可以保留工作汇报，用户需要回答的问题统一送到指定入口。
- `[ctask-supervision-alert]` 到达时先用 `inbox` 核实未过期/已恢复，向用户报告异常与下一步；已有授权足够时不重复追问。超时不表示原会话已停止工作。用户给出新的处理指令后可经 `answer/accept-answer` 转回原会话，保留累计预算和旧请求过期历史，不重做旧 `takeover`；真实恢复后旧告警不可再用于答复。后台监督即使与 hub 同源也会入队通知，不能因继承会话环境变量而静默视为已告知。

## 共享记忆

memory 默认 true，执行器运行前读取偏好和相关记忆，结束时记录有证据路径的摘要。完成后再按全局共享记忆规则沉淀可复用经验，避免把测试成功误写成所有需求都已满足。
