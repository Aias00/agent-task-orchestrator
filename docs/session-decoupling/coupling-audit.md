# 会话依赖审计：contracts + Codex adapter 阶段

完整目标与全部验收仍在 [task.md](task.md)。本记录覆盖已经实现的中立契约与当前 Codex 边界，不宣称 durable transport/recovery、真实 Pi、CLI hub 或最终候选交付阶段已经完成。当前是隔离候选，没有激活运行版、修改真实会话、提交或推送。

## 受信任依赖图

```text
workflow / interaction / recovery / control / delegation
                      |
                 session_router
                      |
            agent_registry (code-owned)
                      |
          agents._CodexAdapter composition
                      |
           codex_session_adapter
              |                 |
       read-only exact index   appserver.Client / queue CLI
              |
        bounded native log decoding

message_bus -> code-owned native_queue_serialization capability
transport   -> registered send() -> normalized DeliveryResult
codex_legacy -> public forwarding compatibility surface only
```

八个核心模块 `control.py`、`recovery.py`、`interaction.py`、`delegation.py`、`workflow.py`、`session_router.py`、`message_bus.py`、`transport.py` 不导入 `codex_legacy`、`appserver` 或新 vendor implementation；AST 行为测试检查这一点。`agents.py` 是固定受信任注册边界，组合工作执行与会话适配器，而不是由任务声明动态插件路径。环境变量只用于定位存储或兼容会话，不提供 v2 actor 权限。

## 当前具体依赖表

| 区域 | 现在调用的边界 | 原生实现所有者与兼容策略 |
| --- | --- | --- |
| Workflow origin | router 的兼容 identity/locator，或显式 opaque reference 的 project verification | UUID 解析与 `CODEX_THREAD_ID` 读取只在 Codex adapter；保留 `origin_thread_id`，不把 locator 记成 actor authentication。 |
| Hub / 问答 | router 的 target verification、delivery checkpoint、send、normalized delivery fields、actor compatibility check | 核心先持久化 dispatching 再发送，保留 canonical question ID、原路由、原授权来源。不会建立第二套问题权威。 |
| Recovery | router 的 recovery binding、checkpoint、send、observe_recovery | `rollout`、`rollout_offset`、`scan_offset` 是旧记录兼容字段；核心不读取或解析原生日志。恢复前的 STOP、原合约、请求次数、期限与代次判断不改。 |
| Controller | router 的 activity、observe_reply、delivery checkpoint 和 send | 旧的全文件 native lifecycle 扫描已移到 adapter，改成有界反向读取。缺失、损坏或超出观察上限的活动证据为 unknown，不作为闲置。 |
| Failure projection | router.activity | 失效/未知活动不能证明没有处理者。保留一次告警和普通状态静默的既有策略；不增加 observer 或监督权限。 |
| Delegation | router 的 coordinator capability preflight、created reference、checkpoint、send | 创建、命名、index lookup 和 readback 全部由 adapter 执行。保留默认 Codex delegation fingerprint、旧 UUID、原任务目录和 public CreatedSession 字段。 |
| Mailbox | code-owned `native_queue_serialization` capability | bus 不再按 `kind == codex` 选择释放策略。受信任 adapter 完成原生 enqueue 后可释放发送槽，unknown envelope 仍不可重放；stream/RPC adapter 不自动释放。 |
| Native format | `codex_session_adapter.py` | 独占 index schema、receipt regex、rollout 事件、app-server 方法及创建策略。SQL 使用只读 URI 与精确 UUID，不查询 latest session。 |
| Public compatibility | `codex_legacy.py`、`recovery.session_path()`、原核心调用签名 | 原有入口转发到 router/adapter；保留继承测试和 fixture 字节，不靠修改旧测试移除兼容需求。 |

## 状态根与证据强度

`ato_config.py` 集中解释 `ATO_HOME` 与兼容 `CODEX_HOME`。ATO 状态与 Codex 原生状态可分开，缺省仍保持旧布局。切换 locator 不迁移或覆盖数据。hub/origin 等既有持久记录里的 `codex_home` 仅保留来源兼容字段，不能授权操作。

旧 index/header 只含 UUID 时，旧 raw-UUID 入口仍能验证原会话。v2 注册另外要求 header 的 cwd 与目标工程精确一致；缺 cwd 不能升级为已验证的 v2 project identity。Codex v2 actor.authenticate 保持 unknown，native load/resume 保持 unsupported，没有因安装成功、旧环境变量或工作执行能力升级这些声明。

`session.verify/create/transport/observe/activity` 现有具体实现分别给出代码证据；activity 方法可返回 unknown。`session.cancel` 尚无已验证实现。turn_completed 是会话事件，不更新工作流 criteria 或人工验收。

## 本阶段验证及剩余边界

新增 `tests/test_codex_session_adapter.py` 使用临时 Git repo、独立 ATO/native 配置、伪 UUID、本地 queue subprocess 与脚本化 app-server 对象，覆盖身份/工程、收据、创建策略与 readback、忙闲与 unknown、精确回复、generation cursor、兼容问答/控制/恢复、native queue capability。它们不是对真实 Codex daemon、云模型或真实用户决策的认证。

外部冻结 `acceptance.py codex` 由 scheduler host 执行；worker 不写外部 baseline/checker/evidence，也不把本地结果当作 host 验收。本阶段不执行真实 Pi smoke。完整 transport fencing、owner lease、恢复故障矩阵、实际 Pi 协调端、CLI hub 和最终迁移/回滚包仍须后续冻结阶段分别实现与验收。
