# ATO 统一入口实施基线（T0）

## 隔离分支与提交来源

核对时 `main` 指向 `86f19d9ff85a025caab1b815a7c70b0ad7b23ef1`。审阅分支
`fix/review-findings-20261122` 指向 `20621c2325954d9863bee1779a1edb8433f73957`。
隔离工作树的实施分支 `feat/ato-unified-offline-20261122` 在 T0 修改前指向
`cc0800db2567c66a164ba922db3ecf4dd9c68b05`。实际 Git 父链如下，四个审阅修复均保留原提交对象。

```text
86f19d9ff85a025caab1b815a7c70b0ad7b23ef1  main 基点
  → 4223d2cc6df0b412ed0d57cee40e20845f33cebb  宿主命令与 Hub 检查共享执行权
  → 291830d78a48b86b69e41f56a5ea95c279af0285  复核预算与累计记账
  → 0b82bfd67e3b06b626916e63b74c983d814a43a6  Pi TLS 建连后改用授权截止时间
  → 20621c2325954d9863bee1779a1edb8433f73957  全绿基线仍执行首个工作阶段
  → cc0800db2567c66a164ba922db3ecf4dd9c68b05  将批准计划带入隔离实施分支
```

设计分支 `docs/ato-unification-task-plan` 的提交
`ac4224c0f043c058b9c4e791f054b02c259acdc0` 直接以同一 `main` 基点为父提交。
实施分支与设计分支的共同祖先为该基点，两处计划文件内容相同。实施分支上的计划提交只改动计划和文档索引。
四项运行时修复均沿用上述原提交。T0 补充基线文档及索引，并修正下文两处测试夹具。
[批准的 T0 实施计划](ato-unification-task-plan.md#t0-整理实施基线)

## 审阅修复与回归

### 宿主命令与 Hub 检查共享执行权

`4223d2cc6df0b412ed0d57cee40e20845f33cebb` 使 `budget-run` 和 Hub `GuardedWorkflow.verify_only()`
在仓库锁内获取同一执行租约。已有有效 origin/session-host 租约，或租约过期但原生活动未知时，
两条入口均拒绝执行。已核验空闲时可接管，旧租约凭据受代次和 fence 约束。运行中续租，退出时释放本次租约。
[宿主操作与交接](worker-permissions-and-handoffs.md) · [H1 离线实现](h1-offline-implementation.md)

以下 4 项均在隔离 worker 中通过。[`SessionV2RecoveryTests`](../tests/test_session_v2_recovery.py)

- `test_host_entries_refuse_active_origin_and_session_host_leases_without_mutation`
- `test_host_entries_refuse_expired_unknown_native_activity_without_mutation`
- `test_host_entries_reconcile_expired_idle_lease_and_fence_late_release`
- `test_host_entries_publish_renew_and_release_their_exact_execution_generation`

### 复核预算与累计记账

`291830d78a48b86b69e41f56a5ea95c279af0285` 在只复核路径用既有 workflow 状态计算剩余总预算，
从同一时点累计执行及清理耗时。`enforced` 模式耗尽时进入 `limit`，`advisory` 模式记录超估。
单项检查限时、STOP 和人工验收门槛继续生效。[H1 离线实现](h1-offline-implementation.md)

以下 4 项均在隔离 worker 中通过。[`HubLoopTests`](../tests/test_hub_loop.py)

- `test_review_only_enforces_total_deadline_and_accounts_cleanup`
- `test_review_only_carries_elapsed_between_runs_and_refuses_exhausted_check`
- `test_review_only_advisory_records_overrun_without_changing_policy`
- `test_review_only_hub_STOP_accounts_and_cleans_the_active_check`

### Pi TLS 慢响应与本次授权截止时间

`0b82bfd67e3b06b626916e63b74c983d814a43a6` 在 TLS 握手后清除原 socket 和 TLS socket
上的建连闲置超时。响应头和 SSE 分块等待仍受本次 CONNECT grant 的绝对截止时间及取消信号约束，
请求仅执行一次。[W2-1 Production Pi 离线实现](w2-1-offline-implementation.md)

以下 3 项使用本地 TLS/CONNECT fixture 检查 10 秒 grant 内的 2.7 秒延迟和 1.5 秒 grant 的超时。
本次 worker 执行时，3 项均在建立合成服务的 `127.0.0.1` 监听 socket 时被沙箱拒绝
（`PermissionError: [Errno 1] Operation not permitted`）。行为结果待宿主离线门禁实测。
[`ProductionTransportTests`](../tests/test_pi_production_transport.py)

- `test_slow_headers_within_absolute_grant_succeed_without_retry`
- `test_stream_idle_gap_over_two_seconds_within_grant_succeeds`
- `test_slow_real_tls_response_is_cancelled_by_absolute_deadline_without_retry`

### 回归已绿时仍执行首个工作阶段

`20621c2325954d9863bee1779a1edb8433f73957` 在首次 `task-run` 预置并执行冻结的首个阶段。
规格无阶段时从冻结目标和原命令检查生成 `hub-initial`。规格同时缺少预置阶段和命令检查时，
以 `INITIAL_WORK_CHECKS_REQUIRED` 拒绝启动。`task-review-only` 仍然只做检查和审阅。[H1 离线实现](h1-offline-implementation.md)

本次 worker 的 2 项回归通过。[`HubLoopTests`](../tests/test_hub_loop.py)

- `test_initial_worker_blockers_are_not_masked_by_green_regression_checks`
- `test_initial_work_without_preset_requires_independent_command_checks`

以下 2 项在 `ready_for_user_review` 断言处得到 `cleanup_blocked`。追踪合成 Reviewer 启动时，
worker 沙箱下的 `sandbox-exec` 返回 71 和 `sandbox_apply: Operation not permitted`。
单独诊断确认预置和自动生成阶段各执行一次，均产出请求的文件，原命令检查通过，冻结规格和原仓库
未变化。只复核路径的原命令检查也通过，未调用 Worker 或 planner。三条路径的人工项均保持
`needs_input` 且无确认记录。完整审阅及终态断言待宿主离线门禁实测。
[`HubLoopTests`](../tests/test_hub_loop.py)

- `test_initial_work_runs_with_green_regressions_with_or_without_preset`
- `test_review_only_green_worktree_never_runs_initial_work`

## 宿主基线失败与修复

上一轮宿主 T0 门禁实际运行 887 项，报告以下 2 项失败。Worker 在 umask `077` 下复现两项失败，
在 umask `022` 下两项通过。临时目录的实测模式显示，`mkdir(mode=0o755)` 在 umask `077` 下
会创建 `0700` 目录，恰好满足 ACP stderr 与 Hub 账本的私有目录校验。

- `test_insecure_directory_symlinks_and_hardlinks_are_rejected_without_truncating_target` · [ACP stderr 回归](../tests/test_acp_stderr.py)
- `test_hub_list_empty_is_read_only_and_rejects_indirect_or_nonprivate_roots` · [Hub 账本回归](../tests/test_hub_foundations.py)

两处夹具都在目录创建后显式设置 `0755`。原拒绝断言、ACP 链接目标内容断言和运行时权限校验
保留。修正后，两项在 umask `077` 和 `022` 下均通过。扩展运行 ACP stderr 与 Hub foundations
共 28 项，通过 26 项。真实 macOS 权限夹具因 `sandbox_apply: Operation not permitted` 得到
`CLEANUP_BLOCKED`。真实进程组 STOP 夹具也得到 `CLEANUP_BLOCKED`，诊断确认沙箱拒绝启动
用于核验进程组成员和身份的 `ps`。两项待宿主复验。

## 核心与全量离线门禁

以 `86f19d9ff85a025caab1b815a7c70b0ad7b23ef1` 对当前工作树运行 `git diff --exit-code`，
以下八个 Python 模块及两个 fixture 的字节均相同。

- `control.py`、`recovery.py`、`interaction.py`、`delegation.py`、`workflow.py`、`session_router.py`、`message_bus.py`、`transport.py`
- [`w2-core-ast.json`](../tests/fixtures/w2-core-ast.json) 和 [`architecture-baseline.json`](../tests/fixtures/architecture-baseline.json)

以下 3 项结构检查均通过。架构扫描得出根模块及含 CLI 的运行时模块各有 8 对双向依赖，均为
冻结基线集合的子集。冻结 AST 和 vendor import 检查覆盖全部八核心。

- `test_O10_core_AST_vendor_imports_and_frozen_CLI_bytes` · [`test_hub_loop.py`](../tests/test_hub_loop.py)
- `test_module_distribution_and_generic_vendor_free_core_ast` · [`test_pi_production_profile.py`](../tests/test_pi_production_profile.py)
- `test_mutual_dependencies_include_cli_and_introduce_no_new_pairs` · [`test_architecture_structure.py`](../tests/test_architecture_structure.py)

Worker 使用 Python 3.12.15、Node 24.22.1、临时 Git 仓库和临时 ATO/native home。
两处夹具回归、可在 worker 内执行的审阅修复回归与结构检查合计 15 项，全数通过。
161 个受版本控制的 Python 文件编译通过，本文及文档索引的本地链接检查通过。
夹具修正后，ATO 宿主执行冻结的 T0 门禁，`.venv/bin/python scripts/run_tests.py` 在
232.556 秒内运行 887 项，零失败、零错误、零跳过。此前在 Worker 沙箱内无法运行的本地 TLS、
审阅隔离和进程组夹具已包含在宿主结果中。协调会话另用 `077` 和 `022` 各复验两个权限夹具，
四次定向检查均通过。此次范围为源码与合成 fixture。[离线测试入口](../scripts/run_tests.py)
