# 文档索引

源码和离线 fixture 是当前可复验的入口；原生 CLI、真实 provider 与用户最终验收分别判断。本文仅分类索引，不移动旧文档、不激活运行态。安装与全量测试见 [仓库 README](../README.md)。

## 现行指南

当前接口、操作与安全边界；具体能力限制仍以对应状态文档为准。

- [Task-based execution budgets](adaptive-budgets.md)
- [Adding a trusted adapter](agent-adapter-guide.md)
- [Adapter and transport boundary](agent-transports.md)
- [设计与实现](architecture.md)
- [Compact origin host handoff](compact-origin-handoff.md)
- [Compact failure notices](compact-failure-notices.md)
- [Final completion notification policy, 2026-11-19](completion-notifications.md)
- [Independent mailbox and transport primitives](message-protocol.md)
- [Hub notifications only when user intervention is required](notification-policy.md)
- [Source publication and runtime upgrades](runtime-upgrade.md) — `scripts/release_snapshot.py` 的只读检查、隔离 stage、显式 activate 与仅入口/controller 回退
- [Neutral CLI human interaction (candidate only)](session-decoupling/neutral-cli.md)
- [User closure and failed-task observation](task-disposition.md)
- [Worker permissions and normal host handoffs](worker-permissions-and-handoffs.md)
- [Worker action ownership and conflict handling](worker-action-policy.md)

## 能力状态

源码基线、支持矩阵、未合并候选及当前整改计划；历史候选的阶段通过不等于现行运行态已升级。

- [Stable baseline and development snapshots](development-status.md)
- [W3 四个历史候选快照源码审计与处置建议（已评审通过，仅定性未实施）](w3-candidate-snapshots-audit.md)
- [H1 可换长期主控（AGY / Claude / Codex）与隔离执行闭环设计（已批准，前置 Spike 通过）](hub-orchestrator-design.md)
- [H1 前置只读 CLI 能力实测 Spike 记录（S1–S4）](h1-cli-spike.md)
- [H1 离线实现、独立 CLI 与 O1–O10 验收边界（待评审，未启用）](h1-offline-implementation.md)
- [Delivery validation boundaries](message-delivery-validation.md)
- [Agent Task Orchestrator (ATO) 整改方案](remediation-plan.md)
- [P3-1 工具链与 ACP 适配方案（已批准，隔离实施）](p3-1-toolchain-design.md)
- [W2-1 ProductionPiProfile 设计（已批准，未启用）](w2-1-production-pi-design.md)
- [W2-1 锁定 Pi 前置离线 Spike](w2-1-spike.md)
- [W2-1 Production Pi 离线候选与验收边界](w2-1-offline-implementation.md)
- [Capabilities and evidence levels](session-decoupling/capabilities.md)
- [Trusted Codex session compatibility](session-decoupling/codex-adapter.md)
- [Managed Pi ACP coordinator](session-decoupling/pi-coordinator.md)
- [Source and capability map](source-map.md)

## 历史验收档案（只读参考）

保留任务合约、阶段检查点、旧迁移方案与历史复现记录。它们可能依赖未发布的 operator-evidence，不作为日常安装步骤或新的执行授权；旧测试计数和外部资格不能推广为当前版本验收。

- [Reproducing the offline ACP timeline](acp-process-demo.md)
- [ACP candidate staging and migration](acp-process-migration.md)
- [Integrate ACP process evidence and trustworthy outcomes](acp-process-task.md)
- [ACP process candidate](acp-process.md)
- [Compatibility, staging and rollback](agent-abstraction-migration.md)
- [Agent-neutral long-task management](agent-abstraction-task.md)
- [Local agent abstraction candidate](agent-abstraction.md)
- [Source consolidation, 2026-11-20](publication-20261120.md)
- [Migration and rollback (not performed)](recovery-escalation-migration.md)
- [Authorized complete objective](recovery-escalation-task.md)
- [Recovery diagnostics and observation-only successor delivery](recovery-escalation.md)
- [Recovery mechanism source release](recovery-release.md)
- [Reference assessment](reference-hardening/assessment.md)
- [Local candidate and operator handoff](reference-hardening/operator-handoff.md)
- [Agent-neutral session coordination candidate](session-decoupling/architecture.md)
- [会话依赖审计：contracts + Codex adapter 阶段](session-decoupling/coupling-audit.md)
- [Neutral CLI hub stage checkpoint — 2026-11-19](session-decoupling/hub-checkpoint.md)
- [Candidate migration and backward compatibility](session-decoupling/migration.md)
- [Origin host qualification and reversible delivery](session-decoupling/operator-handoff.md)
- [Managed Pi coordinator stage checkpoint — 2026-11-19](session-decoupling/pi-checkpoint.md)
- [Durable recovery stage checkpoint — 2026-11-19](session-decoupling/recovery-checkpoint.md)
- [ATO 会话层解耦：完整任务约定](session-decoupling/task.md)
- [Durable transport stage checkpoint](session-decoupling/transport-checkpoint.md)
- [Qualification evidence and limits](session-decoupling/validation.md)
- [验证与复现](validation.md)

源码发布时的固定指纹清单：[source-baseline-20261120.json](releases/source-baseline-20261120.json)。历史打包脚本只在操作者显式提供私有输入后使用；缺输入时返回非零并说明用途，不启动服务或迁移任务。
