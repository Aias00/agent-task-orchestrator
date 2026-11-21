# ATO 会话层解耦：完整任务约定

## 完整目标与边界
用户认可六步方案并要求“好的开始改吧”。完整交付是可验证、可迁移、可回滚的本地候选：独立任务管控、通用会话接口、厂商封装、真实Pi协调端、中立CLI问题答复、故障验收。不是仅改文档、改名、mock适配器或只让Pi做worker。当前主控仍为origin，现有hub不改。最后准备实际切换方案；本次不切换已运行的全局ATO、不Git提交/推送。

基线来自当前实际运行Python文件和现有整合源码；外部baseline.json保存真实哈希，旧source-manifest.json可能为历史包，不当成当前源码证据。正式入口/唯一controller和现有业务任务保持。所有测试在新临时repo、独立ATO/CODEX配置和伪会话进行。继承tests/全部字节受保护，只能新增行为测试。实现兼容层应保持旧测试；如存在不可兼容设计，提交真实原因而不是修改受保护检查。依赖复用只读integrations/pi-acp/node_modules；不npm/pip安装，不读原生认证/旧私有会话目录。

## 统一接口与核心边界
复用agent_registry.py、session_router.py、message_bus.py、transport.py，禁止再建平行消息系统。核心task/workflow/controller/recovery/interaction/delegation/transport不直接import codex_legacy/appserver或解释Codex数据库/rollout/CLI回执。允许适配器/兼容边界内部调用；storage配置集中处理CODEX_HOME兼容位置，新增显式ATO_HOME。不要只移动函数/改字符串而保留核心厂商数据假设。

会话引用为版本化ATO opaque ref，携带adapter/native identity/project/provenance；user/hub、origin、worker、sessionhost角色分开。能力细分session.verify/create/transport/observe/activity/cancel/load或resume/actor authentication；unknown/unsupported显式；不由安装成功、worker能力或ACP end_turn推断协调能力。已有接口做兼容扩展，不能让fixture注册声称真实供应商已支持。Actor验证需本机私有凭证/端点及工程/合约/代次校验，环境变量仅定位，trusted local host不是恶意root进程隔离承诺。

消息状态：queued/dispatching/delivered或delivery_unknown/accepted/responded/turn_completed；工作流验收另外判定。持久化发送屏障、唯一请求ID、dispatch attempt fencing、endpoint/route/contract/execution generation/turn关联、去重回执；旧事件留历史不能结算新代次。unknown delivery不自动重发。不要把managed mailbox与旧question ledger都当作独立投递权威；一条问题保持原canonical ID、路由和授权来源。厂商原生排队的释放规则抽成已验证能力，移除message_bus里的kind==codex分支但保留原保守语义。

执行归属覆盖阶段worker、origin宿主工作、托管sessionhost及恢复；持久租约/代次与仓库锁协作。托管host仅为会话传输生命周期，不是新增业务supervisor/repair/observer权限或无限重试服务。已有活动owner、STOP、失效代次、过期请求、投递未知时不重复执行。无法证实会话闲置为unknown。原会话轮次结束不等于工作流完成。新native Pi进程的启动/取消/重启必须只管自己拥有的进程组，不能扫杀其他会话。

## 真实 Pi 与 CLI hub
现有acp_pi仅worker，此次新增真正受ATO管理的Pi协调会话路径：明确创建、精确ID、两轮持续输入、关联事件/工具、私有有界日志、进程重启。实际固定Pi1.5.2和已有pinned pi-acp依赖，命令/环境由可信配置决定而非任务任意argv。无任意TTY注入。网络/文件/工具权限不因切换Agent扩大，不允许Pi协调模型获得执行ctask管理命令或读取内部令牌的自由shell；通过受限、schema校验的host动作/受信任CLI完成协调任务/ask等。

实测capability支持native load才能调用；否则checkpoint_reconstruction要写清新native session、旧引用、完整目标/当前文件/前次结果、累计历史，不能假称原生续聊。恢复前验证权限与STOP；无法安全确定外部副作用不重放。

提供无需Codex的CLI hub：inbox/read/respond/accept-answer等现有命令可兼容扩展；绑定local endpoint身份及角色，实际用户答复有source，adapter自动输出不能变成用户批准。为真实用户提供可运行的注册/绑定/查询/答复示例。本机hub现有绑定不动。

## 冻结行为验收
外部命令：PYTHON OPERATOR_EVIDENCE/ato-session-decoupling-20261118/acceptance.py MODE。模式与新增suite（独立宿主执行，0失败0skip）：
- contracts：tests/test_session_v2_contracts.py >=8；身份、工程不匹配、缺能力、actor凭证、可序列化cursor、未知activity/unsupported resume。
- codex：tests/test_codex_session_adapter.py >=8；exact旧UUID/index、原queue回执、busy/reply、创建读取权限、已知失败vsunknown、兼容旧流程；并AST检查八个核心模块不能直接导入vendor实现。
- transport：tests/test_session_v2_transport.py >=12；发送前后崩溃、未知不重发、重复ack、伪造/错endpoint/message/session/turn、旧generation、双owner、持久重建。
- recovery：tests/test_session_v2_recovery.py >=10；STOP、过期请求、origin重新活跃、租约失效/迟到回执、worker/host竞争、原失败/attempt保留、无有效处理者准确分类、一次告警、主控静默。
- pi：tests/test_pi_session_coordinator.py >=8；用真实本地ACP subprocess fixture覆盖创建/两轮/工具/身份/断开/native error/取消/重启；fixture与真实Pi严格分开。禁用codex命令、db、app-server的非Codex路径必须有端到端fixture。
- hub：tests/test_neutral_hub.py >=8；通过实际CLI/受信任临时端点走workflow/ask/显示实际问题/用户答复/accept/继续/独立验收；错误actor/代次拒绝、静默不presented，Codex服务全部不可用。
- regression：全部继承+新增tests，>=386，0失败0错误0跳过，所有继承test/fixture哈希相同。现有advisory配置不得污染enforced fixture，外部gate用临时CODEX_HOME/ATO_HOME。嵌套sandbox/socket在worker中失败时不要反复耗轮次，ready_for_checks交独立host。

## 仅最终阶段的 origin 宿主交接
准备scripts/verify_pi_session_local.py --output PATH --local-only，由外部real_pi检查在宿主运行，最多240秒；worker不能调用真实provider。必须是实际已装Pi1.5.2及pinned adapter（记录binary/adapter哈希）加临时loopback脚本化模型端点、dummy credential、空私有原生配置，无外部网络/云端调用或原生auth读取。该脚本需对Codex命令/db/app-server设置失败探针，走本次实际candidate的托管Pi协调端和CLI hub、至少两轮、一次真实文件工具、精确问答/轮次/重启、实际文件与独立检查。自动化测试中的synthetic user答复仅限标注清楚的fixture，不给任何真实任务授权。保留安全有界的逐步收据供origin独立核对，不复制全文prompts/正文/凭据；证据可落build/session-decoupling/real-pi-local/，不能只有一个布尔成功报告。

输出proof字段见外部gate.real_pi：kind=actual_pi_cli_with_scripted_loopback_model；codex_disabled/isolated_configuration/identity_verified/session_created/two_turns_correlated/question_answer_roundtrip/tool_observed/artifact_verified/independent_checks_passed/restart_verified/no_external_calls=true；pi_version/pi_binary_sha256/adapter_sha256/native_session_id/evidence非空；restart_mode=native_load或checkpoint_reconstruction；workflow_completed_by_end_turn=false。不假装云端模型效果或原生resume已通过。

最终prepare_host_delivery任务在offline代码及包就绪后返回needs_host，说明该脚本和精确argv；本workflow host_handoff仅授权origin执行这一次有界local smoke和冻结验收。root真正通过全部criteria才完成。遇到无认证/真实依赖缺失先证实，不偷用旧凭据或添加realprovider调用。

## 包、迁移与回滚
docs/session-decoupling/{coupling-audit,architecture,capabilities,migration,operator-handoff,validation}.md，更新README/仓库内skill（不安装）及adapter指南。给旧任务/未完成消息/未知投递/人工作答/STOP/用户closure/successor关联的逐项兼容策略。只增中立引用/明确映射，不重写固化合约/旧UUID、不重绑已发问题、不清历史。新版本schema保持可回退或明确可读边界；不要回滚state/sqlite快照覆盖新写入。

scripts/prepare_session_candidate.py生成并--check独立核验build/session-decoupling/candidate.json：runtime_activated=false，baseline_sha256为外部baseline真实值，files为所有候选核心/新增adapter/测试/文档/skill/scripts相对路径sha256；不包含任务状态、原生auth、原始会话、node_modules链接。记录基线真实脏diff和本任务delta，禁止把旧source-manifest当成新验收。明确当前哪些验证是fixture、实际Pi+mock、云端未测。准备可review的精确激活/回滚步骤，但不执行切换。调度continue/attempt计数重构、全目录改名、Web UI、真实Claude协调端在本轮之外；Claude保持明确unsupported。
