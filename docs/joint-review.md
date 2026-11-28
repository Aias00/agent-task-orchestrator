# 多方联合审阅

状态：2026-11-28 实现完成，测试待用户通知。仅做源码实现与人工静态审查，未运行测试、构建、provider、真实 DB 迁移或部署。设计基线 `main@9f8ddfa`。用户要求“设计方案并实现”，本阶段不授权发布、部署或真实审阅调用。

## 目标与首版边界

一个 ATO Task 可配置 2–8 位具名 reviewer。每轮各方独立审阅同一份 worker 产物，宿主保存每方结论和可追溯的合并意见，再统一决定通过、退修或受阻。旧的单 reviewer 配置和任务继续可用。

首版顺序调用各 reviewer，所有成员必选，采用 `all` 策略。它避免改造现有单个 child PID 的安全停止协议，也使 mutation tripwire 可以定位到具体成员。成员身份、证据与会话相互隔离；顺序不代表后方能看到本轮前方结论。此版本不实现多数票、LLM 仲裁人、成员可选、自动换模型或并发调度。不因某方已 approve 就掩盖其他方的问题。

## 配置

沿用顶层 `reviewer`，增加互斥的 panel 形态。旧形态含 `agent`；panel 含 `members`。禁止混用旧形态专属字段与 panel 字段。

```json
{
  "reviewer": {
    "policy": "all",
    "max_review_rounds": 10,
    "checklist": ["核对目标覆盖、兼容性和失败恢复"],
    "design_docs": ["docs/joint-review.md"],
    "members": [
      {
        "id": "correctness",
        "agent": {"kind": "codex"},
        "timeout_seconds": 600,
        "session_mode": "fresh_only",
        "checklist": ["状态机、竞态与幂等"]
      },
      {
        "id": "security",
        "agent": {"kind": "claude"},
        "timeout_seconds": 600,
        "session_mode": "fresh_only",
        "checklist": ["权限边界、凭据和只读约束"]
      }
    ]
  }
}
```

- 成员 ID 符合现有 ID_TOKEN 规则，唯一、稳定；允许同 kind 不同 ID/模型/检查职责，不能以 kind 当身份。
- 成员支持现有四类 reviewer：codex、claude、agy、codex_acpx。agent/model/effort、session_mode、allowed_kinds 和单次 timeout 复用单 reviewer 校验。ACP 的 fresh_only、宿主模型默认及不确定结果约束保持。
- panel 层只放 policy、max_review_rounds、checklist、design_docs、members。policy 仅 `all`（可默认）；max_review_rounds 默认10、范围1–50，按整组轮次计算。公共检查项与成员检查项同时生效。
- CLI/REST 可继承每成员300秒默认；MCP validate/submit 必须显式填写每个 members[i].timeout_seconds（5–3600），缺失报稳定字段错误并指向成员，必须在新 admission 前拒绝。历史幂等回放仍先于新参数校验。
- worker 的超时仍独立，不能把某位 reviewer 的超时当整组总时限。展示每成员 timeout/source 和顺序执行方式。
- normalize 与 MCP/REST 完整嵌套 schema 使用同一限制；新版默认配置不能改变旧 spec digest。旧单 reviewer 的默认、回执和 adapter 合约保持兼容。

## 一轮执行与裁决

1. Worker 完成后先运行任务已声明的 checks 和 guardrails。产物及审阅配置均取确定指纹。
2. 创建整组审阅记录（run、round、product/config digest），持久化全部成员 pending，再按声明顺序执行。
3. 每成员只得到任务目标、同一产物、公共/成员 checklist、固定设计文档和上一完整轮次的问题。不会把本轮其他方输出灌入提示词。不同成员的 session、runtime、格式重试、日志及 ACP request ID 独立。
4. 每次调用后先核对 STOP、所有者、产物指纹与 mutation tripwire，再保存成员结果。不得在审阅过程插入 worker 修改。成员有效返回 blocked/request_changes 时继续收集其他成员；执行错误、STOP、mutation 或 ACP 不确定外部状态时立即停止本轮，剩余成员标记未执行并保留原因。
5. 只有全体成员都对同一份产物和同一份有效配置给出有效 approve，才能 aggregate approve。裁决优先级：STOP/执行错误/未完成/blocked > request_changes > approve。blocked 与执行失败不能当作弃权，也不能触发 worker 猜测性修复。
6. 全体有效且无 blocked，但任一 request_changes：汇总所有 blocker 交给 worker 统一退修；下一轮全体重新审阅新产物，不能只复审提出问题的人。整组计一次 review round，不吃 worker 执行失败次数。保留现有 max rounds 和重复无进展保护。
7. 审阅通过不代替 checks、guardrails、manual acceptance。复审通过后仍须遵守原子结算、STOP 优先和 ACP cleanup 门禁。

同一事项出现 approve 与 request_changes 时，以后者的 blocker 为修复要求，其他方 approve 仅保留为各方证据。具体修复建议矛盾时不生成“多数同意所以忽略”的结论；保留来源与完整要求，由 worker 结合代码处理，无法同时满足时按现有 needs_input/blocked 流程明确问题。

## 问题汇总与容量

原始 verdict 永久保留，不改写单方意见。汇总 finding 使用宿主生成的稳定 ID，附带来源 member ID、原 finding ID 和引用。仅对 file、lines、category、severity、title、evidence、required_fix 全部规范化相同的 finding 合并来源；只同位置或标题但不同证据/修复要求的意见不能折叠。不同成员同名 finding ID 不能相互覆盖。

不要把整组最多1024个 finding 塞回单 reviewer 最多128条的 wire schema，或静默截断 blocker。聚合层使用独立结构，保存所有意见与来源；状态回执有界、显示总数及分页入口。worker 反馈如需限长，应明确截断并提供可读取的完整问题文件，且确认该路径在 worker 实际 sandbox 中可读；否则以清晰容量原因暂停，不能丢掉问题后批准。所有公开字段沿用脱敏与 repository scope，不能泄漏 runtime 绝对路径、session凭据、完整原始日志。

## 持久化、停止与恢复

建议追加 review_groups / review_members 表：组保存 task/run/round、product/config指纹、策略、状态、汇总结论与时间；成员保存稳定ID、顺序、有效配置指纹、state、verdict、error、session和时间。成功单方结果逐个落盘；完整组结论再写入现有 reviews 聚合视图，旧单 reviewer 历史保留。失败与未完成的组也必须可查询，不能由 latest_review 中旧 approve 掩盖。

ACP 现有 UNIQUE(run_id, round) 不能容纳同一轮多个 ACP 成员；需要安全、事务化地升级为包含 member identity 的唯一键，或提供等价独立命名空间。既有行关联 legacy identity，保持 request_id、dispatch、receipt、cleanup 与历史语义。不能仅在 prompt 中加名字就认为隔离完成，也不能用假 round 绕过公开轮次语义。所有相关查询都应审核成员身份、未清理状态与任务所有者。

顺序执行沿用现有 PID/birth identity 和 process-group 清理。STOP 后不得启动下一个成员，不得结算 approve；异常进程退出留下未完成组，由 observer 如实报告。resume 不复用旧未完成组的部分 approve，建立新 run/组并对当前产物重审；历史结果保留。ACP outcome unknown 继续走只读核对/清理门禁，不自动重新发出不确定请求。

`recheck` 必须调用整组审阅并保留各方结果。`recheck --checks-only` 只有找到同 product/config、全体 approve 且所在 run 已正确完成的完整组才能沿用；更换成员、模型、检查项、策略、超时或产物都使旧整组通过失效。先前旧单 reviewer 不能满足 panel 所需批准。

## CLI、API 与 MCP

- `status` 的 legacy `review` 保持单 reviewer 兼容；panel 增加策略、组状态/轮次、当前成员、各成员摘要、最终/部分结果标识与总数，未完成不能看起来已批准。
- 新增分页只读 `ato project reviews TASK_ID`，支持明确组/成员过滤和有界游标/条数；可读取历史组、成员详情和 findings，不依赖截断后的 trace。
- 同一授权 service 暴露 `ato_reviews` MCP tool 和等价 REST 操作，沿用现有路由模式、repo白名单与read权限；客户端文档/模板更新，勿绕过 scope 或新增公网入口。仅增加只读操作，无额外 mutation API。
- `ato agent binding` 显示全体配置。现有 `ato agent reviewer ... --kind` 不能把 panel 静默替换为一个 reviewer：panel 时要求 `--reviewer-id ID` 修改已有成员；单 reviewer 旧命令继续可用。修改须 idle/revision 检查并重新验证完整配置，不能与运行中的组竞态；配置摘要变更使旧批准失效。首版组的增删在新 task spec 完成，不新增整组运行时热编辑。
- 文档、安装skill、MCP discovery schema、validate effective config、CLI帮助和示例同步更新。当前已部署MCP需后续单独更新，源码实现不代表上线。

## 实施及待授权验收

完整实现配置/schema、持久化升级、独立执行与聚合、执行器/复审/STOP、查询与主控入口、文档及测试代码。允许按仓库惯例调整内部结构，但不能削弱上述状态和授权语义。

本阶段不执行测试、构建预演、真实 provider、服务重启或DB迁移；可写离线测试。后续验收至少包含：

| 场景 | 预期 |
| --- | --- |
| 旧spec和旧DB | digest、单reviewer回执不变；升级保留旧审阅/ACP历史 |
| 2–8成员及同kind | ID唯一；参数互斥；完整嵌套schema；MCP逐成员显式timeout |
| 全通过/一方退修/一方blocked | 确定性all裁决，完整收集有效结论，统一修改后全体复审 |
| finding冲突与容量 | 原始证据不丢失，同ID不同来源不覆盖，超过128不误通过 |
| 成员timeout/格式错误/STOP/写入违规 | 精确归因，无静默降级/弃权，剩余成员不启动，组不通过 |
| 会话与ACP隔离 | 同kind模型各异、双ACP成员不冲突、未知结果不重放 |
| 进程退出与resume | 历史部分结果可查；不复用半轮approve；owner fencing和cleanup保留 |
| recheck与checks-only | 仅完整同产物同配置组可沿用；配置变化必须重审 |
| CLI/MCP/REST | status与分页history一致，read scope和仓库隔离，历史幂等兼容 |
| 回归 | 旧worker/单reviewer/重试/无进展/通知/独立checks行为保持 |

后续仅获测试授权后执行 `python3.12 scripts/run_tests.py`、带官方SDK的server套件及传输检查。测试全部使用临时 ATO_HOME/CODEX_HOME 和离线fixtures；真实provider资格另行授权。实现报告必须注明“实现完成，测试待用户通知”，并列出未完成或设计偏差。


## 实现映射（2026-11-28）

| 设计层 | 实现位置 |
| --- | --- |
| 互斥单方/panel 配置、2–8 成员、旧 digest | `ato/spec.py` |
| MCP 成员显式 timeout、REST 默认、完整嵌套 schema、旧幂等回放 | `ato/server/spec_schema.py`、`ato/server/service.py` |
| 全体 pending、成员逐次落盘、完整组原子聚合、旧 DB 升级 | `ato/store.py`，schema version 3 |
| 顺序独立执行、all、无损来源合并、容量暂停 | `ato/joint_review.py`、`ato/reviewer.py` |
| 会话、每成员/格式重试 runtime、独立日志 | `ato/adapters/base.py`、`codex.py`、`claude.py`、`agy.py` |
| ACP `(run_id, round, member_id)`、request 精确读取、owner/cleanup | `ato/store.py`、`ato/adapters/acpx.py` |
| worker 统一退修、全体复审、STOP、recheck/checks-only、原子结算 | `ato/executor.py` |
| 状态/历史/原始成员意见/聚合意见/ACP 摘要、分页与脱敏 | `ato/joint_review.py`、`ato/cli.py`、`ato/server/service.py`、`ato/server/http.py`；MCP 从 manifest 自动发现 |
| idle + worktree lease + revision CAS 的成员编辑 | `ato/cli.py`、`Store.set_reviewer` |
| 离线测试代码 | `tests/test_joint_review.py`、`tests/test_joint_review_service.py`；既有 ACP、SDK、schema 测试补充 |
| 操作文档与安装 skill | `docs/cli-and-spec.md`、`docs/mcp-api.md`、`skills/ato/`、`examples/joint-review.json` |

实现细节与验收交接见 [联合审阅实现报告](joint-review-implementation.md)。
首版选择设计允许的容量暂停分支：完整 worker 反馈超过 64 KiB 时以
`REVIEW_FEEDBACK_TOO_LARGE` 暂停，全部意见仍保留在 DB 和分页查询中；不创建
未经 sandbox 可读性验证的外部反馈文件。reviewer 完整提示词超过 128 KiB 时
以 `REVIEW_PROMPT_TOO_LARGE` 暂停，同样不截断设计文档或历史 blocker。
