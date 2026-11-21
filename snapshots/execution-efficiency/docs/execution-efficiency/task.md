# ATO后台效率与责任闭环：完整任务约定

## 目标、基础与边界
用户2026-11-19在五项具体实现方案后确认“可以开始做”。完整本地交付包含：固定任务运行版本、任务级责任处置、专属worker连续轮次、可核验检查点、按影响范围复用验收、准确分项计时与受控前台/旧ATO/新ATO对照；全部接入实际Runner/Workflow/Controller/CLI，而非独立helper或文档。当前hub/origin保持本主控。此次不部署这批新功能、不Git提交推送、不迁移真实旧任务或改变权限/尝试/STOP/repair/监督窗口。

基线为已实际completed/all5passed的Linger workflow20261119-032425-7f6af654，完整677项0失败0跳过；manifest2256534fb9f58ec277f18511b5662479a8da82aa0433a58657a121b200bc02f6。196源文件和53测试/fixture字节受外部baseline保护；不编辑前驱固定检查/合约。前驱包含已实现的v2分类计账/静默task_view/容量排队，复用这些机制，不能退回较旧session候选覆盖它们。此任务与正在独立执行的cb025会话解耦部署无版本依赖；外部检查仅允许那一次已授权精确入口转换，不把当前全局入口永久冻结。原正式运行文件/队列guard/已装skill不改。

## 一：固定运行版本
创建新任务时，在合约外的版本化、代码所有的runtime-selection记录中，持久化实际runner路径、解释器、source manifest指纹、来源和兼容版本。stage、planner、worker、host-check、resume重启均按本任务的绑定启动，不从可变全局ctask重新挑版本；已有进程继续固定旧目录。不能让模型/计划任意提供import/command覆盖。记录修改、源文件/依赖漂移、错误project/contract/generation、越权actor必须拒绝而非偷偷重绑。

既有无binding任务保持明确legacy兼容路径，不改原JSON/contract/来源/计数或隐式迁移。新入口升级只影响未来选择；用户STOP、旧claim和待答复canonical ID不变。新增runtime_selection.py并保留只读load(directory)接口，返回可审计runner/python/manifest信息；实际CLI端到端应证明从另一个默认入口启动仍使用任务原版本。源码包不会因测试用另一入口而被修改。回退只改选择，不能恢复旧SQLite覆盖新写入。

## 二：任务级责任与诊断闭环（VPS案例）
沿用control数据库/现有recovery与execution ownership，不另建业务调度器。新增责任记录绑定workflow、project、contract、execution generation、角色和明确actor、expires_at/next_action/证据；ordinary origin会话active或unknown只能表示可观察性，不能作为“正在处理这个workflow”的证据。真实执行owner、受控host工作和有效任务处理claim应可区分；合法claim有界，不借heartbeat无限续期/加接管权限。claim需要现有已验证origin/hub角色，环境变量仅定位。

诊断完成必须形成明确disposition：原授权内的行动及执行证据，明确外部等待条件/有限复查触发，真正需要用户的canonical问题，或带真实依据的关闭。诊断reply/queued/heartbeat不等于业务恢复。recovery=false继续不自动修复/重试；不要把普通origin active当作可无限延期的处理人。

failure episode稳定绑定实际失败尝试/检查和原合约，elapsed/heartbeat/一般会话忙碌不制造新失败代次。通知投递/临时抑制/呈现/stale和业务是否解决分开；一条notice stale不清掉仍blocked任务的处置责任。resolved须任务级真实后续进展、有效已通过后继或真实有据关闭。保留未知投递不重发、一次无人失败提示和普通更新静默。记录具体reason codes及哪个谓词使旧提醒失效，避免泛OR文案无法追查。inbox空不掩盖followups待办。

为独立宿主验收公开failure_attention.failure_episode(directory,state)->dict，至少包含真实失败任务/attempt/check/criteria签名，排除纯计时/heartbeat字段。新增action.failure_episode可校验元数据漂移；没有可比语义证据的旧记录保持unknown/需核对，不能无条件忽略所有哈希不符。旧presented/claimed记录不重新通知。

## 三：专属worker连续轮次
扩展现有agent_registry/session_router/managed transport能力，保持厂商细节在适配器。先实现默认Codex worker精确native ID连续轮次，保留其它已知能力的诚实边界；已有Pi管理接口可复用，但其本地模拟证明不能冒充Codex worker原生恢复证明。每个工作流/worker具有独立会话，不复用用户/hub/origin聊天，不跨工程、合约、权限或运行版本混用。

以代码选定、实测支持的能力决定原会话续办；支持才给同一native ID传增量任务/变化，使用完整已授权目标+阶段增量，保留有限上下文摘要。已装Codex CLI help支持exec resume SESSION_ID，但帮助不等于此适配器权限/恢复已实测；不得使用--last或只据安装声称成功。不支持/失效时可明确checkpoint_reconstruction，新native ID/来源/累计历史可追溯；不能说成原生resume。重启前核实STOP/owner/进程清理/unknown消息屏障。继续使用原sandbox/approval/profile，warm resume不得更宽；容量准入不等于写权限。

实际Runner/Workflow在显式versioned worker_session_policy启用时接入，多轮同任务不重复整个访谈/代码扫描；旧模式保留。支持至少两个受控轮次、精确turn/session/消息关联、重启可观察与结果终结。Agent end_turn/EOF仅轮次结束，整体仍由独立checks接受。持久传输的delivery_unknown不自动重放或另起native会话掩盖。

## 四：增量检查点与有界收尾
按阶段持续保存：完整目标、已修改文件的安全指纹、当前步骤、宿主实际已通过checks及对应输入、剩余事项、native/session/turn、权限/运行版本、累计invocations/failures/continue/wait历史。使用有界私有原子写/唯一序列号，恢复需验证实际文件、权限及证据仍匹配。摘要文本/心跳不作为进展或完成证明，禁止把凭据/原始prompt/stderr/整段历史抄进检查点。

在既有单次硬超时前留可配置收尾提前量；支持checkpoint.request/steer等经过资格验证的能力才发送有界请求。否则只通过原worker prompt明确提前收尾；不任意TTY注入、不新增超时repair。成功保存continue不计失败；硬超时、坏JSON和真实失败仍计失败，保留原限额；后轮从有效检查点接续，不重新做全部已验证工作。只有精确进程清理确认才允许接手；输出格式失败不能隐去副作用未知。支持细粒度故障：写前/写后崩溃、半JSON、重放checkpoint、伪造/过期回执、错误项目/代次、旧校验结果失效。

## 五：按输入指纹复用独立验收
新增显式verification_policy.version=1，checks按实际task/criterion ID声明{inputs:[相对文件或目录],env:[变量名],cacheable:true|false}；解释器/argv、脚本/工具链及依赖版本、contract和权限边界、输入内容/modes自动进入可信指纹。未知/不完整依赖、路径越界/链接、改变执行环境或缺失证据时保守重跑，不把声明不全的缓存当正确性保证。敏感环境不原样落盘，使用白名单hash/明确unknown。

stage仅跑受影响门禁，未变且证据完整有效的检查复用时保留原run、源码指纹、执行时间、returncode及真实日志来源；失败和pending不能缓存成passed。外部实时状态、人工作答/ack、需要新鲜度的health/权限/自然业务门禁不缓存，TTL过期或包/环境变化必须失效。最终完整集成回归保留新鲜独立执行；不得靠删检查/改原argv/降门禁来提速。默认无policy旧任务仍原行为。

新增verification_cache.py提供可信核心；测试必须走实际Runner.checks和Workflow check_total，证明同输入缓存命中、输入/脚本/环境/合约/权限变化失效、final强制重跑、缺日志拒绝、外部/manual拒绝缓存。不是仅做一个没有调用者的hash helper。

## 六：分项计时与同条件对照
复用已有v2 accounting、task_trace、task_view，以span/parent关系区分queue/warmup/model-request或unknown、tool、independent_check、external_wait、checkpoint/restart、总wall。不能把worker进程整体时长说成纯推理；缺原生usage或model latency就显示unknown，父子span不得重复累计。queued/diagnostic_reply不等于执行。status/task-summary显示完整目标、当前stage/真实进程/能力、旧独立验收与新worker自检、未解决外部缺口、谁负责/下一步。

受控fixture benchmark比较同一初始文件、同模型协议/延迟脚本、相同权限和最终验收的三组：前台直接adapter、旧ATO cold worker、新ATO warm worker。至少2轮每组，记录模型请求数、native sessions/startups、上下文/工具读取量（能测才报）、checks真实执行/复用数、active/wait/总wall；证明真实原生轮次/文件结果一致并降低可避免的cold restart/check次数。fixture不能宣称真实provider更快，不给无证据速度倍数。

最终origin宿主另跑一次明确有界实际已装Codex CLI对照：三组、每组两次样例、每样例两轮，共最多12个native turn、总1200秒；仅临时Git repo和小文件任务，沿用现有已配置模型/认证/权限，不读取或复制auth正文、不修改原生用户配置、不新增服务/生产操作。各组同模型参数/权限和独立checks，真实精确session验证，必须包含默认Codex warm连续会话证据；实际样本允许输出无明确延迟提升，但不得伪称加速，应报告计数/范围/噪声。由scripts/verify_execution_efficiency_local.py --output PATH --existing-profile --max-turns 12实现，worker只准备/fixture测试不执行实际模型；外部acceptance.py host_sample --run-origin由origin审阅后调用，成功证据绑定精确最终manifest。同candidate只复用成功证明，不自动开新窗口；unknown/失败保留证据交origin具体核实，未跑不能算全部通过。1200s是卡死保护，不是时间预算许可；不新增业务自然观察窗口。

## 验收、包和迁移
新增tests/test_runtime_selection.py>=12、test_task_responsibility.py>=16、test_worker_session_reuse.py>=16、test_incremental_checkpoint.py>=12、test_incremental_verification.py>=14、test_execution_efficiency_metrics.py>=12。必须含真实CLI/temp-repo/子进程整合和竞态/故障用例；外部固定host_behaviors另测已知缺口及Runner缓存，不允许改外部检查。继承53文件字节不改；完整discover>=759，0failure/0error/0skip。保留677基线真实日志和所有旧失败。允许仅sandbox限制交原host门禁，不放宽权限或重复卡死。

准备可重现portable candidate.tar.gz及candidate.json（完整source/tests/docs/modes、baseline/predecessor哈希、runtime_activated=false），scripts/prepare_efficiency_candidate.py --check独立验证及提取后验证，无auth/tasks/deps/私有历史；更新README/仓库skill但不安装。docs/execution-efficiency/{architecture,migration,validation,operator-handoff}.md给出旧任务/计量/缓存/能力的版本兼容、精确部署回滚和真实未测试项。历史前驱proof仅说明它的原bytes，本候选必须自己完整通过全部criteria和一次actual Codex worker proof。本次只交候选与操作步骤，不替换当前部署、不提交推送。
