# Linger 参考机制的 ATO 后续改造

## 完整目标及独立范围
用户在2026-11-19 Linger参考评估后明确“好的可以继续”。先前会话解耦workflow 20261118-231351-195e655a 已由origin完成九项正式验收，包括真实Pi1.5.2+本地脚本化模型两轮/工具/中立问答/检查点重建，非云模型，未部署。本任务从其精确候选复制136文件，外部baseline保存全部来源；不是旧任务续验或attempt重置。

本任务交付三个相依功能及全量复验的本地候选：分类记账，清楚可查询的任务详情，随后有界静态容量调度。不要只交文档或孤立helper；实际Runner/Workflow/CLI调用必须接入。不激活运行版、不提交推送，不对真实旧任务做迁移。本轮不做Web界面、动态自适应算法、强制抢占、实时steer、额外agent能力、shell权限或新模型调用。

## 第一阶段：正常续办与失败分开记账
为新创建的显式选择execution_policy.version=2任务提供新语义，老计划无此字段保持旧行为/公开计数。规范最小policy：{version:2,max_continuations:6,max_waits:4,max_yields:4,max_no_progress:3}，支持其他正整数取值；拒绝未知/非整数/非法字段。limits.max_attempts在v2是失败上限；attempts/attempt_sequence仍保留所有真实启动次数，绝不清历史，新增execution_accounting统计invocations/continuations/waits/yields/failures/active_seconds/wait_seconds等，不能把attempts显示为failures。Workflow向阶段传递原固化policy。

正常continue先保存可复查检查点及宿主验证的进展指纹；不得把尚未通过最终checks的continue计作失败。等待waiting不占失败次数，记录真实原因及有限下次检查时点，等待期间推进独立分支。可支持明确yielded结果，在实际进程清理确认且无未知投递时释放本轮资源，计yield不计失败；不强制抢占。外部等待/排队不能计入active_seconds，保留旧总elapsed账本及真实等待。

真实非零退出、超时、坏structured outcome、宣称complete但验收失败计失败，失败上限不可超。blocked/needs_host/manual仍独立；unknown delivery不得由v2自动重发。continue/wait/yield有独立总次数及无进展护栏；不得用这些标签得到无限重试。进展须由宿主核实（安全文件检查点/实际source指纹/独立验收变化，摘要不是进展证明），不能扫描凭据/完整历史，不能把未验证输出当进展。重复resume/host-checks不重复收费、扣失败次数或自动清attempts；旧任务只读映射unknown，不能伪造0失败历史。

## 第二阶段：静默任务视图
扩展status的task_view并增加task-summary DIRECTORY --format json|text。保留原JSON字段和脚本兼容。按完整目标、当前阶段、真实owner/worker存活、验收剩余、各类计数/active/wait、阻塞原因、下一步、needs_user、原blocked与已核实successor、能力未知及恢复状态显示。状态是查询投影，读不启动模型、不唤醒worker、不投递通知、不更改已读/授权/STOP/完成状态。对部分通过、无进程残留running、有效外部等待、无人接手失败分别如实显示。旧attempts不冒充失败。

普通进度/完成/宿主交接保持静默；真实需要人决定/权限/补充/验收仍走原canonical ask；一次无人处理失败提示保持原去重。文档给常用状态示例，无强制弹窗/默认Meta唤醒。

## 第三阶段：有界容量，交互预留，非抢占
先固定可配置的endpoint别名容量和interactive保留槽。trusted ATO_HOME/capacity.json配置version=1/endpoints={alias:{limit:N,reserved_interactive:R}}，0<=R<N；别名不能包括URL/key/token。显式plan.capacity={endpoint:alias,priority:interactive|background}选择，嵌入Workflow阶段原配置，未选择容量的旧任务不自动迁移。不能由worker或plan覆盖全局容量上限。已有运行任务不干预。

实际执行前有持久去重排队和原子claim，queue等待不启动模型/不计失败/active，STOP可取消排队，重启不重复发。交互优先且独立预留槽；后台FIFO并提供等待/饥饿可见性，不声称强公平/自动抢占。release必须具备精确token/owner/generation和cleanup确认；无确认/未知状态保持占位或隔离，不能仅TTL超时挪给另一个worker。无resumable能力不抢占/虚构恢复。释放容量不解除仓库锁或任务owner权威，不获得写权限。与原unknown消息屏障、STOP、外部等待和进程清理整合。

稳定供验收的最小接口在capacity.py：CapacityStore(path,clock=callable); configure(endpoint,limit,reserved_interactive); enqueue(endpoint,request_id,priority,owner,generation); claim(request_id)返回含token的grant或None; release(request_id,token,cleanup_confirmed=True)返回bool; cancel(request_id,owner,generation)返回bool; snapshot(endpoint)返回可读dict。同ID不同owner/generation拒绝，重复同请求不得产生新lease。使用临时库跨独立连接/进程实际验收。

## 独立验收与交付
外部acceptance.py不可修改，保留48个前驱测试/fixture原SHA256、当前launcher/运行Python/只读依赖。每阶段新增真实行为tests：test_continuation_lifecycle.py至少10、test_task_summary.py至少10、test_capacity_queue.py至少12（含实际Runner/Workflow整合而非仅helper）；外部固定测试进一步检查CLI语义、查询无副作用、容量持久化/错误token等。全量discover至少前驱563项+新项，0失败0错误0跳过；worker沙箱限制交宿主门禁，不当代码通过。

实现新prepare_linger_candidate.py及--check，build/linger-continuation/candidate.json含runtime_activated=false、baseline_sha256、predecessor_manifest_sha256、所有可移植source/test/docs SHA256与modes，排除任务/auth/deps状态。生成可重复archive，原包/基线保持历史。文档architecture、migration、validation、operator-handoff，更新README和仓库内skill但不安装。写明旧任务选择/数据版本兼容、回退不覆盖新数据、实际验证与未验证。origin final host-checks核实全部criteria，不能把阶段完成等同整体完成。任何上线/发布另行具体授权。

## 固定来源与许可
Linger revision 8d02fe8c73b8289211b5581b483a2c16cfd76918，原源码与assessment在OPERATOR_DEPENDENCIES/ato-linger-reference-20261119。仅参考机制自行实现，不复制带额外托管/branding条件的代码，不安装上游，不把docs算法宣称为实测效果。
