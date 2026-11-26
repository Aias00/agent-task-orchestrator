# 验证与复现

## 本地测试

```sh
python3.12 -m unittest discover -s tests -v
```

测试只在临时 Git 工程、本机假会话数据库、fixture CLI/队列及模拟 daemon 中运行。不要求云模型账号，不给真实用户会话发消息，不访问业务服务器。第三方 worker 进程/文件沙箱用例要求 macOS `sandbox-exec`；CI 选择 macOS。fixture 成功不代表真实云端模型效果或账号额度。

| 测试文件 | 验证重点 |
| --- | --- |
| `test_runner.py` | 依赖、返修、等待、停止、独立验收、状态完整性 |
| `test_workflow.py` | 阶段不等于整体、只读规划、最终回归、人工条件、累计上限 |
| `test_recovery.py` | 单次接管、过期、预算、真实恢复、多进程去重 |
| `test_interaction.py` | hub/origin 校验、答复接收、迟到拒绝、真实fixture队列 |
| `test_alerts.py` | 监督退出前持久化告警、未知发送、失效条件 |
| `test_delegation.py` | 模拟 WebSocket daemon、新会话接受、绑定与单次派发 |
| `test_agents.py` | Claude/Pi协议、缺认证阻塞、沙箱、停止清理、规划器继承 |
| `test_control.py` | 呈现与接收分离、有限提醒、回复关联、续办、真实后台重启去重 |
| `test_progress.py` | 三种CLI事件、重复工具、停滞、部分JSON行、观察状态恢复 |
| `test_install.py` | 含空格路径安装、可运行入口、skill路径解析、防误覆盖 |

## 验证层次

1. **确定性单元/进程测试**：完整可在仓库复现。消息传输和模型输出可控，覆盖崩溃窗口、并发和状态误判。
2. **原生 CLI + 本地模拟 provider**：开发环境曾用实际 Pi 验证工具调用、路径 guard 与完整结果。这个层次仍不能证明云模型指令遵循。
3. **真实 provider 与 workflow**：开发环境曾验证 Pi 真实调用及一个两阶段总任务。不同模型组曾出现末尾换行遗漏与重复搜索，因此不宣称任意provider/模型等效。
4. **业务实测**：两项既有完整业务 workflow 已接入主控；其中一个完整目标已通过全部六项最终条件，主控自动产生完成待报告事件，核查证据后显式关闭通知。另一个业务仍在原会话验收。它们的生产日志、账号、会话和真实数据没有收入仓库。

发布前从整理后的源码重新执行完整测试。测试结果通过不扩大业务验收范围，不代表新增生产权限、持续高负载认证或人工交付确认。仓库中不保存业务日志来充当可复现fixture。

## 手动检查一项真实任务

```sh
ctask status WORKFLOW_DIR
ctask inbox
ctask control status
ctask followups
```

同时检查总任务、当前阶段、真实处理进程、监督、待办与来源。只见 `queued` 不能说原会话已响应；只见 `accepted` 不能说已恢复；只见阶段 `completed` 不能说完整目标完成。结果通知需要核对所有最终验收日志，向用户说明后才 `presented`。

本机停止 controller 只停止观察，不停止业务 worker；`unsupervise` 只关闭业务监督，`stop WORKFLOW_DIR` 才请求停止具体任务。操作前区分这三个层次。

## Long-task skill and notification release (2026-11-17)

The source release passed all 230 local unittest cases in an isolated test Codex home, with zero failures or skips. Coverage includes advisory/enforced time policy, permission and normal host handoff boundaries, selected frozen host rechecks retaining attempts, pending-question delivery, non-intervention hub suppression, direct queue ingress and legacy installer behavior. The skill format validator and local reference checks passed. This is local validation; cloud Actions results must be reported separately.

Fixtures use fictional origin/hub UUIDs. No live task state, queue bindings, authentication, provider configuration or business logs are part of the release. Installation of the optional direct-queue guard requires an explicit local launcher choice and retains the native executable. It does not install automatically with the ordinary task-runner launcher.
