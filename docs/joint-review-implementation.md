# 联合审阅实现报告

2026-11-28，任务 `task-82fb630a7bd04986ae60065ff6931da0`。
**实现完成，测试待用户通知。** 本轮只修改当前隔离 worktree 的源码、离线测试与文档，
并人工阅读差异。没有运行测试、语法编译、构建、真实 reviewer、传输检查或服务；
没有安装、部署、迁移真实 DB、merge/push、发送外部消息或派发代理。
此报告不代表测试、真实 provider 资格或人工验收通过。

## 交付与静态核对

详细文件对应见 [设计的实现映射](joint-review.md#实现映射2026-11-28)。

- 配置：单 reviewer canonical 形态不增字段；panel 支持 2–8 位稳定 ID 成员、
  `all`、公共/成员 checklist、冻结设计文档。完整嵌套 schema 与 normalize 共用
  单方参数限制；MCP 新 admission 逐成员要求显式 timeout，旧幂等回放仍在前。
- 持久化：schema 3 的组/成员表在调用前一次性写入全部 pending，逐成员保存原始
  有效 verdict、错误、会话、时间和配置指纹。只有整组完成才原子写入组结果与
  `reviews` 聚合视图。ACP 旧表在事务内重建，原请求、dispatch、receipt、cleanup
  保留，旧行使用空 legacy member ID，新唯一键为 `(run_id, round, member_id)`。
- 执行：成员按声明顺序读取同一产物与配置；本轮其他成员的结论不进入提示词。
  每成员会话、runtime、格式重试和日志独立；ACP 请求精确读取并沿用 PID/birth
  清理协议。有效 blocked/退修继续收集；错误、STOP、写入违规、外部结果未知
  停止后续成员。整组退修只计 review round，worker 得到完整合并意见，全体复审。
- 证据：只有全部规范化字段一致才合并 finding，保留来源 ID；同 ID 不覆盖、
  不把最多 1024 条意见塞回 128 条单方 wire schema。分页可读全部原始/聚合意见、
  checked items、residual risks 和成员 ACP 摘要；不公开 native session、runtime
  绝对路径或原始日志。状态明确区分组完成、run 完成和批准是否可沿用。
- 门禁：启动/落盘/聚合/结算核对 owner；STOP 不会成为 approve。结算重新核对
  产物及完整组批准，并检查所有未解决 ACP 请求。进程崩溃保留部分记录，读视图
  标记 incomplete，resume 从新 run 全体重审。checks-only 仅复用同产物/同配置、
  所在 run 已 completed 的全体批准；旧单方或半轮批准均不满足。
- 操作入口：`ato project reviews`、`ato_reviews`、既有服务上的 REST reviews 路由
  共用授权与分页实现。成员编辑必须指定 `--reviewer-id`，在 worktree lease 下做
  idle/revision CAS 并验证全配置；完成后改变配置会转为 `REVIEW_REQUIRED`。

## 设计细化与偏差

没有引入并行进程管理、投票、仲裁、可选成员、自动换模型或 reviewer mutation API。

1. 采用设计允许的容量暂停方式：worker 完整反馈上限 64 KiB，reviewer 完整提示词
   上限 128 KiB。超限明确报错并保留全部问题；没有外部反馈文件，因此不依赖
   未验证的 worker sandbox 可读路径。这是首版容量选择，不是 blocker 截断。
2. 历史查询以 `section` 区分组、成员、findings/checked_items/residual_risks；单方
   旧记录另用 `legacy`/`review_id`。每页 1–20 项；列表是 ID 游标，详情是数组偏移。
3. 崩溃后的 running 组保持原始 DB 记录；状态和历史在 owner 不存活时显示
   incomplete/OWNER_NOT_LIVE，避免观察器替崩溃执行器补写裁决。已保存成员可查，
   pending/running 原始状态保留，不猜测未知外部操作。恢复不续用该组。
4. ACP 的 provider session ID 是各独立 bridge/request 的局部身份；不同成员以
   run/真实 round/member/request/native home 联合隔离，不要求不同 provider
   进程绝不返回相同的局部 session 字符串。没有改动 ACP fresh-only 或 host 默认。
5. finding 规范化保留路径和代码缩进，只统一标题空白、证据/修复要求的 Unicode
   NFC 与换行；不同证据/修复要求不折叠。
6. 超时来源单独存放，不参与 spec canonical；配置默认不会改变旧 spec digest。
   原先没有来源证据的历史配置显示 unknown，不猜测为用户显式设置。

## 待授权离线验收

以下命令**尚未执行**。获得测试授权后，在当前 worktree 运行；前两项脚本自动使用
临时 ATO_HOME/CODEX_HOME 和合成仓库，第三项也只启动隔离临时传输服务。
SDK 依赖须已具备；本轮没有安装依赖。不要设置指向真实 provider 的测试覆盖变量。

```sh
python3.12 scripts/run_tests.py
python3.12 scripts/run_tests.py --server
python3.12 scripts/check_server_transport.py
```

| 场景 | 主要测试代码/需核验结果 |
| --- | --- |
| 旧形态 digest 与旧 DB | `test_joint_review.py`、`test_core_and_store.py`；旧 canonical 不变，ACP 行无损、升级可重复 |
| 2–8 同 kind 成员、互斥与 timeout | `test_joint_review.py`、`test_joint_review_service.py`、`test_mcp_spec.py`；稳定成员字段错误、admission 前拒绝、旧回放兼容 |
| all/退修/blocked/全体复审 | `test_joint_review.py`；顺序、成员提示词隔离、公共职责、原始意见、review 计数 |
| 去重/冲突/1024 finding/容量 | `test_joint_review.py`；全部来源保留、全页可读、超限不再启动 worker |
| 格式错误/timeout/STOP/mutation | `test_joint_review.py`；准确成员错误，pending 成员不执行，不写 approve |
| owner、崩溃与恢复 | `test_joint_review.py`；部分证据可查，旧 approve 不满足新 run，owner 丢失不结算 |
| 双 ACP、未知结果、清理 | `test_joint_review.py`、`test_acpx_reviewer_integration.py`；同 round 不冲突，独立 dispatch/native home，unknown 禁止重放 |
| recheck/checks-only/配置编辑 | `test_joint_review.py`；整组重审、product/config/run 门禁、运行时编辑拒绝 |
| CLI/service/REST/MCP 分页 | `test_joint_review_service.py`、`tests/server/test_http_mcp.py`、`test_controller_bundle.py`；read scope、跨 repo/跨 task 隔离、边界参数、脱敏 |
| 全量既有回归 | 原有套件；worker、单 reviewer、checks、guardrails、无进展、通知、PID 安全 STOP 保持 |

真实 provider、现场 DB 升级、已部署 MCP 更新及人工验收分别等待后续授权。
