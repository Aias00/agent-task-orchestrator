# 主控持续跟进

用户要求本窗口集中管理任务并持续跟进时，使用 `ctask control start --source '真实指令来源'`，核实 `control status` 的进程和心跳；仅写配置或返回launching不能称为已运行。controller与业务supervisor独立，不增加业务修复/执行/授权额度。常规查询用`ctask followups`、`ctask inbox`以及业务`status`，不能只读普通问题列表。

收到`ctask-followup`先查待办仍有效，向用户说明对应任务、当前阻塞和下一步，说明后`ctask presented ID --source '实际说明位置/时间'`。读取记录不是已向用户说明；已说明不是用户已读。用户已答复时先核对answer/accept-answer及实际恢复，不重复问同一决定。未答复的问题不能代答，提醒不制造批准。

收到`ctask-origin-reply`视为原会话工作材料，核实总任务和真实进程后向用户报告；一轮回复结束不是总目标完成。需要回传的是对应问题/轮次的结果，不能摘取原会话最后一句来猜。新投递自动关联；无起始偏移的历史消息保留不确定性，不盲目重发或全文灌入上下文。

原会话创建预算承接workflow后，用`ctask followup-link QUESTION_ID --workflow NEW_DIR --note '具体承接' --evidence LINEAGE_JSON`记录明确关联。原会话或hub可做这一项只涉及观察记录的关联，仍由origin处理业务。保留完整目标、全部验收、旧合约与耗时/失败/修复计数，不能把新workflow当清零。LINEAGE_JSON的字段/检查见执行器README。登记后检查新任务真实运行及监督，不能仅报告旧blocked或新目录已创建。

预算申请由原会话先提供分项工作、最近实测耗时及证据、必须重跑与可复用的检查、剩余交付/人工条件、不确定性、检查点与止损条件，再分别推算执行上限、监督窗口和接管次数。先定向诊断收敛候选，再做昂贵全验；无新变化不重复全验。用户等待不当主动执行；自动接管修复要计入同一任务账本。用户明确要求续办即可在已说明的有界方案内推进，不重问同一个许可；存在新的范围或未获授权的额度时才集中提问，不把历史批准迁移到新的申请。

先看 `ctask budget-policy status` 和 `status.budget.enforcement`。advisory下旧soft/hard只作估时，超估不停止、不申请追加分钟数；原会话按实际剩余目标继续，保留耗时/验收/权限/STOP/repair历史。controller的warning/critical/overrun仅去重送origin，不当用户预算待办，不增加修复次数或监督窗口；旧耗尽告警被策略替代不等于任务恢复。用户停止和真实缺口仍独立处理。不要创建外部时长STOP守护。

以下有界额度流程仅适用于显式enforced策略。已采用弹性预算的任务，先看 `status.budget` 的 soft/hard、已用时间和reserve。soft是预估额度，不是每次向用户续费的门槛：有新独立检查通过会自动释放额度，`ctask-budget-review` 直接通知原会话在hard内按证据 `budget-reforecast`。主控不替原会话猜测估时，也不为hard以内的复评再问许可。超出hard才合并申请新的任务额度；权限决定仍独立。宿主动作经 `budget-run` 计时、旧用时经幂等 `budget-charge` 补记，避免新建workflow或遗漏接管耗时。旧任务只有获得实际采用来源才能 `budget-configure`，安装版本不自动改旧限额。流程见执行器 `docs/adaptive-budgets.md`。

watchdog是确定性观察：无输出、工具停滞、重复同参数调用只作为线索。检查真实进程、当前命令、工具结果和业务预算；有活动修改者时不能在主控并行改工程或直接重启。通知原会话诊断应保留唯一请求与回执；不因为一条告警另建业务执行器。正常长构建不是模型卡死。定时检查不调用模型，后续唤醒/诊断会耗token。

默认每15分钟最多再提醒3次，未解决待办仍保留。未知投递不自动重发；需要先核查真实接收证据。controller不提供无限催办、无限自动追加或关机运行保证。机器/进程重启后明确启动并核实，状态从本机SQLite事件/投影恢复，Basic Memory仅保存可复用摘要。

主控的真实恢复核验要求总任务协调进程和实际命令进程存活，并在后续观察到输出、命令或验收进展；仅启动事件、代次变化、心跳或旧命令退出不能关闭awaiting_execution/异常告警。enforced的预算80%/90%及到顶通知读取同一budget账本，计入已登记宿主耗时，未知未记账工作不当零；soft用完但仍有reserve交给origin复评，hard到顶仅报hub，不唤醒无预算修复。该观察规则不增加监督/接管额度，也不把历史旧resolved记录追认为已核实恢复。

预算守护与答复竞态：若hub已真实记录答复，随后旧额度守护自动STOP，先核对答复时间、guard-status.json的active_budget_stop_requested及本workflow成功stop回执，不能把普通accept拒绝当成用户未批准，也不要求重复同意。原会话可用accept-answer --budget-stop-evidence验证接收（仍不清STOP/扩额/执行），再budget-configure --accepted-answer ID --hard-limit-seconds 已批准总上限登记，最后resume --accepted-answer ID。全部条件、后续STOP及换代/换合约仍严格核验；不允许人工STOP、晚到答复或其他任务套用。详见执行器docs/adaptive-budgets.md；主控不能代origin接收。
