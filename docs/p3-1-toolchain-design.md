# P3-1：Pi/Node 工具链与 ACP 适配方案

状态：评审通过，按 P3-1A / P3-1B 实施。2026-11-21 用户明确批准设计提交 `9aa83d6`，
要求 A2、B2 两次独立提交，完成后等待验收再考虑合入 main。本文按
[整改方案](remediation-plan.md) 第 6 节，交付 Batch 4 的设计与验收约定，
用户批准的范围执行；设计基于 `dc08da948bd9a1e5142e6528762be3ea89773644`。

## 1. 目标与本批边界

让 `acp_pi` worker 与 `pi_acp` coordinator 在受信任的不同 Node/Pi 安装位置工作，
把支持版本、源码指纹和协议变换集中管理。保留 native receipt、turn nonce、独立 checks、
STOP、未知投递隔离、累计历史和既有沙箱强度。

建议本批实施两个可独立审查的源码步骤：

1. **P3-1A**：受信任工具链解析、集中版本/哈希注册表，以及两个 profile 与两个 native
   launcher 的一致性接入。
2. **P3-1B**：集中且可验证的 ACP 兼容后端，消除调用方散落的字符串替换；保留一个明确
   标识的 `0.0.34` 兼容实现，并补齐协议与变换破坏测试。

此批验证全部使用临时目录、假 CLI 和本地协议进程。真实 Pi/provider 资格、生产激活、
上游发帖或提交 PR、其他候选分支合入，以及 P3-2 能力实现均不在本批交付内。
`session.load_resume` 继续为 `unsupported`。

## 2. 当前代码事实

| 位置 | 当前行为 | 实施必须覆盖的边界 |
| --- | --- | --- |
| [pi_acp_worker.py](../pi_acp_worker.py) | 写死 nvm Node 路径与 `~/.local/bin/pi`；通过派生脚本启动 ACP | 外层与 native 子进程必须使用同一工具链 |
| [pi_acp_exec.py](../pi_acp_exec.py) | 直接执行 Pi 脚本，Node 选择受 shebang/PATH 影响 | 显式使用已验证 Node 执行已验证 Pi 入口，禁止 shell 拼接 |
| [pi_session_profile.py](../pi_session_profile.py) | 固定 Node、Pi `1.5.2`、adapter `0.0.34`；校验 adapter 源码 SHA；记录 Pi 入口 SHA | Pi 当前只校验版本并记录 SHA，尚无 Pi 哈希准入；不能描述为已有完整供应链校验 |
| [pi_coordinator_exec.py](../pi_coordinator_exec.py) | 从 profile 导入固定 Node/Pi；限制 RPC argv、session 路径 | 更换解析方式，同时保留 RPC 与 session 路径限制 |
| [scripts/derive_pi_acp.py](../scripts/derive_pi_acp.py) | 9 处 `replace_once`，校验原始 bundle SHA 与版本 | 保持未知 bundle 拒绝；为每个变换命名并验证最终输出指纹 |
| [pi_session_profile.py](../pi_session_profile.py) 的 `derive_coordinator` | 基础派生后另有 5 处替换 | worker/coordinator 共用注册表，分别维护输出 SHA 与能力声明 |
| [acp_bridge.py](../acp_bridge.py) | 使用固定版本 ACP SDK 启动，另有私有有界 stderr | SDK 会合并 HOME/PATH 等默认环境；只传少量 env 不等于完全清空环境 |

当前 ACP 原始 bundle SHA256 为
`324aeb8bba1228937e16b1326fb3e014e2a625b2dea549dbcd49b006cd5df6a2`，
由仓库 npm lock 与已安装离线依赖交叉检查。不能因版本字符串相同接受另一个 bundle。
当前源码在 native RPC 事件处理内部生成原生终止回执，并给更新绑定 host turn nonce；
外置 ACP 转发器无法仅凭 `end_turn` 恢复这些可信信息。

## 3. 工具链解析与信任来源（P3-1A）

新增仅依赖标准库的 `pi_toolchain.py`。导入、adapter 注册、普通 CLI help 不探测版本、
启动进程、读取凭据或写状态。仅在受信任 host 准备启动时调用解析器。

### 候选选择

| 工具 | 候选顺序 | 明确配置无效时 |
| --- | --- | --- |
| Node | host 环境 `CTASK_NODE_BIN`；否则过滤后的 host PATH 中首个 `node` | 返回安全错误，禁止悄悄改用另一项 |
| Pi | host 环境 `CTASK_PI_BIN`；否则过滤后的 host PATH 中首个 `pi`；未找到时兼容 `~/.local/bin/pi` | 同上 |
| pi-acp | 仅仓库固定依赖根 `integrations/pi-acp/node_modules/pi-acp` | 不接受项目或环境提供的 adapter 源码 |

候选顺序只在未显式配置且尚未选中候选时使用；已选候选版本/指纹不通过时拒绝启动，
不继续搜索以掩盖错误。host 环境是操作者启动 ATO 时的环境，不来自任务 JSON、模型、
仓库 `.env`、项目配置或远端协议字段。现有 plan 的 `agent.kind/model` 表面保持原样。

解析器执行以下检查：

- 显式路径必须为绝对路径，路径是单一文件参数而非命令行；解析软链接并验证可执行文件。
  PATH 忽略空项、相对项、workspace/runtime 内条目和指向这些位置的链接。校验候选及其
  路径祖先的所有权/可写性，拒绝其他用户可替换的工具。受信任 symlink 可以保留用于定位，
  最终执行固定真实路径；测试覆盖链接与真实路径两端。
- 在隔离临时 HOME、固定 cwd、最小环境下使用 argv 探测 Node `--version`，设定超时与
  stdout/stderr 字节上限，严格解析稳定版本，主版本须 `>= 24`；异常、超时、非零、预发行
  或畸形输出均拒绝。不会执行真实 Pi 做本批验收。
- Pi 执行前先核对注册表内的 package/入口指纹，再用同一 Node 探测 Pi
  `--offline --version`，同样隔离 HOME、禁用更新和可执行配置、限制输出与超时；
  版本输出仅做一致性校验，不是独立信任依据。
- 清除 Node 加载器/预加载相关环境（如 `NODE_OPTIONS`、`NODE_PATH` 及平台动态加载变量），
  不继承任务提供的命令、扩展或 provider key。worker 的既有只读 native auth 定位与
  coordinator 的空 native HOME 分别保持；不得为统一解析复制凭据或扩大网络权限。

### 父子进程一致性

解析结果为不可变 `Toolchain` 数据，包含真实 Node/Pi 路径、版本、指纹及注册表条目 ID。
父进程在已有 `0700` runtime 中原子写入 `0600` 工具链描述；这是启动参数证据，
不是 actor proof、task authority 或新的投递账本。只记录工具元数据，不含原始协议或凭据。

两个 native launcher 只接受 code-owned runtime 下的该描述，核对路径、所有者、权限、
版本条目及当前文件哈希，再执行 `[node, pi_entry, ...固定参数...]`。不得在子进程重新
从 workspace/PATH 选择工具，也不得把 descriptor 当作任意 argv 配置。由模型可写的
runtime 文件不能取得 host 信任；沿用 OS/工具守卫禁止修改受信任启动材料的边界。
fixture 要证明替换二进制或 descriptor 后在 native spawn 前拒绝。相同操作者主动并发
替换受信任工具属于部署边界，哈希复核不宣称消除了所有 OS 级 TOCTOU。

`acp_bridge` 的 SDK 默认会补入 HOME/PATH；worker 必须显式设置选择后的 PATH，且
launcher 始终使用绝对 Node/Pi 路径。两条启动链的实际 argv 均需进程级假 CLI 验证。
coordinator runtime 保持在 workspace 外；worker 位于项目内的 `.codex/tasks` runtime
必须继续受现有 OS 沙箱和敏感路径工具守卫保护。不能满足保护条件的自定义 runtime
在启动前拒绝，而不能仅凭文件 `0600` 就假定模型无法改写。

## 4. 集中版本与指纹注册表

建议新增 code-owned 数据文件 `integrations/pi-acp/compatibility.json`，由
`pi_toolchain.py` 与下一节的兼容后端读取，不允许环境/任务指定其他注册表。
host 必须验证 ATO 源码、注册表、原始 adapter 与工具路径都在模型 workspace/runtime
之外；派生 adapter 则仅存放在受保护的私有 runtime 内。

每个条目包含 schema 版本、Pi package 身份/版本/已审阅构建的入口及包内容指纹、adapter
包名/版本/原始 bundle SHA、适配后端 ID、worker/coordinator 派生产物 SHA，以及
扩展能力集合和验收来源。Node 记录最低版本策略，运行时另外记录实际版本/文件 SHA。
哈希说明其具体对象；入口 SHA 不冒充整个 Node/Pi 依赖树的完整性证明。

初始仅接纳 Pi `1.5.2` 与 pi-acp `0.0.34` 的明确已核实构建，不因此自动增加其他版本。
Pi package 内容清单/指纹需在实施时从可复验的离线发布包审核并登记；现有源码没有
可直接沿用的 Pi 准入哈希，不能把本机任意文件第一次算出的哈希自动变成白名单。
尚未获得可复验 Pi 构建时，真实启动继续拒绝，离线测试使用独立 synthetic 注册表。

新增版本须同时提交新条目、准确输出指纹、协议/安全回归与资格来源；不采用 semver
范围猜测兼容，不在运行时 npm install、联网升级或自动学习哈希。npm lock 的 tarball
integrity、原始 bundle SHA、派生输出 SHA 各自校验不同对象，三者不能互相替代。

## 5. ACP 协议适配边界（P3-1B）

新增 `pi_acp_compat.py`，集中承担注册表选择、输入验证、纯变换和输出验证。
`scripts/derive_pi_acp.py` 保留现有 CLI；`derive` / `derive_coordinator` 保留可调用门面，
profile 不再自行拼接补丁。root 模块不反向导入 profile、worker、runner 或业务核心。
新增模块与数据纳入 pyproject、源码分发清单和文档。

当前唯一后端命名为 `pi-acp-0.0.34-compat-v1`，内部使用显式顺序的命名变换表：

- 基础 9 项：隔离状态目录、禁用更新探测、prompt nonce 传递、排队保留 nonce、
  每轮初始化、settle 原生终止证据、原生 prompt 异常、native event/receipt 处理、
  ACP prompt 元数据入口。
- coordinator 附加 5 项：禁止虚构 native identity、返回真实 identity、更新携带 nonce、
  捕获 emit 时的 nonce、禁用 slash-command 发现。

每项要求匹配点恰好一次，异常只输出后端/变换 ID 等安全信息；缺失、重复、未知源码、
重复派生或输出 SHA 不一致均拒绝，不能模糊匹配或输出部分产物。先在内存中完成所有
验证，再写新的私有临时目录并原子发布；已有输出拒绝覆盖。输入 node_modules 及 lock
不得被修改。相同输入/后端/角色生成逐字节一致产物。

这是有版本边界的兼容后端，仍有源码变换，不宣称已经彻底消除文本补丁。
后续如上游提供可信 native receipt 与 nonce 扩展，可增加无需变换的独立后端；其准入
仍需要同等协议测试。仅在 ACP 外部包装消息或把 `end_turn` 当 native stop 的替代方案
不能满足当前安全契约。向上游发送内容另按实际授权处理。

协议验收须证明以下语义完整保留：

- native `stop`、ACP `end_turn` 与独立宿主 checks 分别核验；错误具有粘性。
- 错 session/nonce、startup/history、旧轮终止、缺失 native receipt 不能证明当前成功。
- STOP、取消、EOF、超时及可能已经发送后的异常保持原来的未知/终止分类，不自动重放。
- coordinator 身份必须来自 native session；不以随机 UUID 或 worker 身份冒充。
- 错误与审计只保留安全元数据；worker 私有 stderr 仍为 `0700` 目录内 `0600`、
  至多 1 MiB，父 stderr/task_trace 无原始协议与凭据。

## 6. 实施依赖、交付与回滚

| 步骤 | 依赖 | 预期交付 | 通过条件 |
| --- | --- | --- | --- |
| A0 | 本文范围确认 | 冻结源码基线；审核初始 Pi 发布包/哈希来源；记录独立验收约定 | 不把本机版本号当准入证据；全部基线用例先通过 |
| A1 | A0 | 标准库解析器、注册表、假 Node/Pi fixture 与异常测试 | 安全解析、无导入副作用、未知工具拒绝；不启动真实 Pi |
| A2 | A1 | 两个 profile、两个 native launcher、私有工具描述和分发接入 | 父子 argv 一致；保留 profile、auth、RPC、沙箱与诊断边界 |
| B1 | A2 | 统一兼容后端、纯变换与 hash/缺失/重复/不可变测试 | 两角色输出等价、指纹固定；输入与旧 runtime 不变 |
| B2 | B1 | 派生 adapter + synthetic RPC 端到端回归及总验收 | 所有既有安全回归与新增异常验收通过后单独提交 |

A2 与 B2 各形成可审查的源码提交；确认后在同一已授权 P3-1 范围内连续推进。
在独立实现分支/worktree 交付，完成报告后等待用户验收，再安排主线合入。
源码回滚使用正常 Git revert，不 force push；发布源码不触发生产更新，旧 runtime 不迁移。
已有可能投递或未知状态的任务不得因切换后端重新发送、重绑或清空记录。

## 7. 实施验收矩阵

| 类别 | 必须覆盖的情况 | 失败时预期 |
| --- | --- | --- |
| Node 解析 | 受信任显式路径优先、非 nvm 安装、带空格路径、Node 24/更高稳定版；23/畸形/超时/输出超限 | 安全非零错误；明确无效配置不回退 |
| 路径信任 | PATH 空项/相对项、项目内工具、软链进入项目或 runtime、非执行文件、可被其他用户替换 | 在运行候选前拒绝；假 CLI 调用标记不存在 |
| Pi 准入 | 已批准构建；同版本不同 hash；未知版本；合法版本输出但包指纹不匹配 | 未批准文件不执行；不自动增加白名单 |
| 启动一致性 | worker/coordinator 的父子进程 argv、恶意 PATH/shebang、Node 预加载变量、描述被改写 | 使用同一 Node/Pi 或拒绝；不得换工具继续 |
| 兼容后端 | 输入/输出 SHA 错、缺/重复匹配点、角色不同、重复派生、未知 capability、失败原子性 | 无半成品，无 node_modules 写入，无宽松 fallback |
| 协议回归 | 原生错误再 end_turn、成功及独立 checks、错 session/nonce、迟到事件、取消、EOF、STOP、未知不重发 | 保留原有 evidence/authority/state 语义 |
| 安全与结构 | 敏感路径读写、私有 stderr、不含凭据的 trace、8 个核心 AST、基础模块标准库、无新增双向循环 | 原断言不降级、不 skip；依赖仍 ≤ 8 对且为旧集合子集 |
| 安装与文档 | 临时 bin-dir 安装；新增模块/数据完整分发；所有 Markdown 相对链接；无后台激活 | 安装保持无副作用，旧 CLI help/API 兼容 |

新增校验所需 fixture 扩充需列入变更说明；不能删除、弱化或跳过既有用例来制造全绿。
所有假工具只写 synthetic 输入并限制执行时长。无需本机登录、hub、生产任务、真实 Pi
或真实 provider。Linux sandbox、cloud qualification、native load 不以这些测试冒认通过。

每个实施步骤结束运行 `.venv/bin/python scripts/run_tests.py`（0 failures/errors/skips）；
最终同时执行全量 `py_compile`、`scripts/check_architecture.py`、核心 AST/分发/元数据/
文档专项。记录准确测试数量、新旧依赖对比、测试源码快照与提交 hash；日志与证据放在
仓库外，不提交 runtime、数据库、凭据或 operator-evidence。

## 8. P3-2 后续独立任务

按 [原能力状态](session-decoupling/pi-coordinator.md) 与
[候选清单](development-status.md) 单独评审，不能与本批工具链改造混报：

| 任务 | 需要单独落实的验收 |
| --- | --- |
| ProductionPiProfile | 凭据只读、出口策略、费用/真实 provider 测试范围与证据；保留 LocalPiProfile 隔离 |
| Pi native load/resume | exact native identity、旧进程终止、代次、幂等和未知效果证明后才能升级能力声明 |
| Claude coordinator | agent_registry v2 的独立 managed transport、identity、工具守卫与 actor 边界 |
| Linux sandbox | 在实际 Linux namespace/bubblewrap 环境验证等价隔离；未具备环境时保持拒绝执行 |
| 候选分支消化 | 每个 snapshot 独立源码评审、新验收及真实历史门槛；不覆盖主线或活动候选 |

## 9. 评审结论位置

建议确认第 1 节的 P3-1A + P3-1B 为下一轮完整实施范围，第 7 节为最终验收条件。
用户已批准上述两步。实现分支基于 `9aa83d6`；不把设计阶段的测试检查通过当作
P3-1 实现完成。生产更新与 P3-2 保持独立边界。

### 既有夹具的必要扩充

- `test_acp_stderr.py::test_pi_worker_passes_private_log_and_only_reports_type_and_diagnostic_path`：
  保留 `PI/NODE/SANDBOX` patch 面，新增标准 `prepare_toolchain` 门面注入 synthetic 工具。
  旧的空白 placeholder 不能通过新增的发布包准入；派生脚本改为真实离线运行，不再被
  subprocess mock 跳过。原私有 stderr、错误类型、路径提示和无凭据泄露断言均保留。
- `test_pi_session_coordinator.py::test_real_launch_construction_private_and_pinned_without_running_pi`：
  保留 subprocess.run mock 和原全部隔离断言，另注入同一门面的 synthetic 结果，避免
  新增 Node/Pi 探测触及真实 CLI。`NODE`/`ROOT` 导出保留，NODE 是只解析路径的惰性门面。
- 新增工具链测试独立验证真实版本探测逻辑、两条 synthetic native 启动链、环境净化和
  sandbox-exec 的写入/别名/重命名阻断；夹具不注册任何生产可选的 synthetic 后端。

工具信任检查也应用于源码安装的祖先目录。其他用户可改写的源码/安装目录会拒绝运行；
例如 group-writable 的挂载根不会被静默放宽。隔离测试复制同一源码至私有临时安装目录
以验证准入，不修改本机挂载权限。发布包自身内容摘要不涵盖另行安装的 node_modules；
依赖安装/lock integrity 和实际 provider 资格继续分别判断。
