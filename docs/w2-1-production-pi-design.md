# W2-1：受管 Pi 协调端 ProductionPiProfile 设计

状态：**设计已获用户批准，离线实现候选未启用、未获得真实 Provider 冒烟授权**。设计基线为已合入 W1 的 `09c0f8f7a7b96cc0ef4226b1cd5b81c0fdb8b6dd`，包含 P3-1 与 W1 `a2c84a6`。本文保留原设计阶段范围和行号；实现状态见[离线候选说明](w2-1-offline-implementation.md)，不是现行 CLI 或云端资格。源码引用行号固定对应设计 commit；链接打开源码文件，`L` 标记给出复核位置。

本轮仅新增本文并登记索引；不改变运行代码，不安装或激活快照，不操作本机入口、真实 interaction home 或在线 controller，不调用云端模型。后续实施与真实 Provider 冒烟分别需要用户评审和单独授权。更早的 [P3-1 设计](p3-1-toolchain-design.md)、[受管 Pi 状态](session-decoupling/pi-coordinator.md) 和 [发布契约](runtime-upgrade.md) 是背景，不能替代本轮验收。

## 1. 源码事实与待补边界

| 已落地模块与基线行号 | 当前事实 | 对 W2-1 的约束 |
| --- | --- | --- |
| [pi_session_profile.py](../pi_session_profile.py) L35–73、L75–120 | `LocalPiProfile` 仅接受 HTTP 回环端点和 `ato-local-dummy`；创建隔离 native home，写入合成模型配置 | 保持校验、调用方式、默认值和本地 fixture 行为不变；生产配置走独立类型 |
| [pi_session_coordinator.py](../pi_session_coordinator.py) L155–165、L217–258 | `pi_acp` 只允许精确的 `LocalPiProfile` 类型；持久化 creating 后才启动，身份不能重复占用 | 显式增加生产身份分支；不能把原精确类型检查改为任意 duck typing |
| [pi_toolchain.py](../pi_toolchain.py) L87–132、L166–171、L266–285 | 可信绝对工具路径、owner/祖先权限、精确 Pi 包指纹、稳定 Node ≥24；环境重建；有限版本探测 | 完整复用，不采用任务 PATH、环境 registry 或未审查 CLI；版本探测不加载用户认证 |
| [pi_toolchain.py](../pi_toolchain.py) L304–368 | `write_launch`/`load_launch` 固定同一 Node/Pi 和 adapter 指纹；`write_exclusions` 排除 trusted、adapter 和 runtime 根替换 | 生产 launcher 仍须经过这些校验，不能另造宽松启动器 |
| [pi_acp_compat.py](../pi_acp_compat.py) L23–28、L52–152、L155–193、L234–278 | 精确 backend/capabilities、9+5 个命名变换、输入/输出哈希、原子禁止覆盖发布 | 继续使用 coordinator backend，禁止 profile 自行字符串补丁、接受任意版本或就地改 node_modules |
| [compatibility.json](../integrations/pi-acp/compatibility.json) L3–15、L18–39 | Node ≥24、Pi 1.5.2、pi-acp 0.0.34 和两个角色输出指纹是代码拥有的契约 | 源码/依赖升级必须重新评审，release archive 审核不等于真实 Provider 资格 |
| [pi_coordinator_exec.py](../pi_coordinator_exec.py) L12–25 | 仅 read 或 read/edit/write；禁 MCP/skills/context/外部扩展，只装固定 guard；当前环境由 `native_environment()` 重建 | 无 Shell/Web/MCP；生产凭据不能靠继承整个宿主环境实现 |
| [acp_bridge.py](../acp_bridge.py) L25–46、L71–108 | worker bridge 的 SDK 日志脱敏按 source pathname 匹配，私有 stderr 仍可保留原始子进程字节 | 生产不能直接复用“私有就可保存原始 stderr”的规则；需检验路径别名、异常和 SDK logger 的脱敏覆盖 |
| [pi-coordinator-guard.mjs](../pi-coordinator-guard.mjs) L7–41 | 每个 `tool_call` 检查私有 turn、STOP、合约/代次和 origin/events 指纹，再判路径 | **当前没有预算校验，也不是任意宿主 action 的统一入口**；生产实施必须补齐，不能将其描述为现有保障 |
| [pi_session_coordinator.py](../pi_session_coordinator.py) L108–141、L360–386、L417–483、L498–550 | envelope、origin、租约和 generation 校验；发送前持久 possible-effect 边界；cancel 后关闭自有进程组 | 保留 unknown/no-replay；生产需补慢请求期间的持续守卫及子进程/后代退出证明 |
| [budget.py](../budget.py) L384–417；[runner.py](../runner.py) L105–119、L141–189 | W1 在软 deadline 重新核对进展；有界 `Popen.wait` 随子进程退出唤醒；STOP/清理仍保留 | 复用预算语义，不把 W1 Runner 的修复误称为已经自动覆盖 async Pi host |
| [pi_session_coordinator.py](../pi_session_coordinator.py) L603–617 | `session.load_resume=unsupported`，即使上游宣称 load | Production 同样 unsupported；W2-2 才单独设计 native load/resume |
| [release_snapshot.py](../scripts/release_snapshot.py) L183–238、L256–291、L322–366 | 固定 commit/archive/tree，全量离线资格、只读封存、inventory/工具哈希复验 | 从已审查快照定位代码；seal 是运行封存，不是抵御 owner 主动 chmod 的权限边界 |

## 2. 显式 opt-in 与严格身份隔离

生产类型拟置于新叶模块 `pi_production_profile.py`，**不继承 `LocalPiProfile`，不放宽其任何判断**。本地 `pi_acp`、fixture kind 和旧任务继续按原路径运行。生产使用独立 adapter kind `pi_acp_production`，固定 `profile_id=production-pi-v1`；注册由可信宿主显式调用，模块 import、发现原生 auth 文件或存在 API key 都不能注册/启用生产能力。

可信宿主配置示意（只含引用，无真实凭据）：

```json
{
  "schema_version": 1,
  "enabled": true,
  "profile_id": "production-pi-v1",
  "adapter_kind": "pi_acp_production",
  "authorization_ref": "reviewed-production-permit",
  "provider_id": "reviewed-provider",
  "model_id": "reviewed-model",
  "endpoint": "https://provider.example/v1",
  "credential_source": {"kind": "native_auth", "provider": "reviewed-provider"},
  "egress_policy_id": "provider-egress-v1",
  "sandbox": "read-only",
  "max_cost_usd": "0.05",
  "hard_timeout_seconds": 60
}
```

开关为这个宿主配置中的 `enabled`，缺省 `false`；必须同时传入精确 `ProductionPiProfile` 对象、匹配 kind 和有效人工授权引用。**没有环境变量自动启用开关，没有任务 plan 中的同名开关**。配置来自工作区外、宿主拥有且不允许其他用户写入的绝对常规文件；拒绝符号链接/不安全祖先、重复 JSON key、未知字段、错误类型（含把 bool 当整数）、缺字段、空值和未知 schema/profile/policy。示例域名不是白名单条目，实际 provider/model/协议与价格上限需来自代码拥有的审查表。

生产端点只允许显式 HTTPS、有效 TLS 验证、固定主机/端口及 API 路径，不允许 URL 用户名/口令、query 凭据、通配域名、模型动态改 endpoint/header、`!command`、任意 proxy 或客户端证书路径。端点、模型、认证方式或出网策略未被离线验证时即拒绝。初版仅批准可用静态 API key 的明确 provider/model/协议组合；OAuth、刷新 token、云 metadata/ambient identity、登录命令与动态密钥命令不在 v1 范围内。

只有创建**新的**受管生产 host/session 时才能 opt-in。冻结的 origin/envelope 必须精确绑定新 identity；profile 非敏感指纹涵盖 schema、profile/kind、工具链/快照、provider/model、credential **引用**、出网与费用/超时策略，写入 provenance/私有绑定并在 dispatch 和控制动作时复验。provider secret 的值、哈希或片段均不参与指纹。旧记录缺 profile 身份时只走旧本地类型规则，不能补默认生产身份。

生产凭据、网络、工具链或授权校验失败，均在网络调用前 fail-closed；不试另一个 provider、不回退 Local、不重绑旧 origin、不迁移既有任务、不更换 kind 冒充相同 session。关闭生产开关只禁止新准入并撤销本 host 的调用许可，已经可能发生的请求保留 unknown 证据；回滚也不会重放旧 turn。

## 3. 原生凭据定位与内存生命周期

本轮只读定位结果：原生默认目录 `$HOME/.pi/agent` 下 `auth.json`、`models.json`、`settings.json` 均存在，为非 symlink、0600 文件；当前调用环境未设置 `PI_CODING_AGENT_DIR`，未发现名字含 `API_KEY`/`AUTH_TOKEN`/`ACCESS_TOKEN` 的环境变量。**只做路径/权限与变量名检查，未读取配置正文、未探测 key 有效性**；不能据此断言存在某 provider 的可用认证。该结果是本机时点观察，不作为其他机器默认值。

后续生产宿主按下列顺序定位，禁止隐式 fallback：

1. 用户显式选择 `credential_source.kind=native_auth` 或 `environment`，两者互斥。native 目录只从可信宿主显式绝对路径，或经确认的 Pi 原生目录规则定位，不能接受任务/模型传入的 `PI_CODING_AGENT_DIR`。environment 模式只读取配置中一个经审查的 provider 变量名，缺失/空值失败；不遍历拷贝整个环境。
2. 新叶模块 `pi_production_credentials.py` 只读打开被选 provider 的 auth 文件（O_RDONLY/O_NOFOLLOW、fstat、owner、0600、常规文件、单链接、大小≤64 KiB、祖先权限及路径排除）；内存解析拒绝重复 key、所选项未知 schema、OAuth/命令/多源歧义。原生文件可以保留其他 provider 的合法项，但不得解析、输出或选用其凭据。原生格式解析器只在这个 Pi adapter 叶模块内，并用固定版本的合成 auth fixtures 证明兼容。
3. 不把原生 home 指向 native Pi 子进程；不复制 auth/models/settings 文件。新 runtime 的 models 配置只由审查表生成，凭据字段只能是该固定 Pi 版本已验证的**环境变量引用**，绝不能是解析后的 key，也不能是会缺失回退为字面值的未验证表达式。确切引用语法必须在 pinned Pi 1.5.2 离线测试中确认，不能从另一个开发版 Pi 的文档推断。
4. 秘密只存在宿主内存和确需认证的可信 ACP/Pi 进程最小环境中。宿主使用不可序列化、repr 脱敏的 `SecretLease`；在最终 Popen/exec 前才构造临时 env。`Launch.environment`、provenance、state 和通用 runner event 只持非敏感字段；不能把 SecretLease 交给 dataclass/asdict、trace 或异常 formatter。当前 `PiCoordinator.start()` 直接传 `launch.environment`（L223–229）需要独立生产启动分支，不能将秘密先写入可打印的 Launch 对象。
5. 当前 [native_environment](../pi_toolchain.py) L383–391 不传 provider key；保持本地行为，拟新增的 `pi_production_exec.py` 先复用 clean/native environment，再仅合并审查表允许的一个认证变量。真实 key 通过进程内 env 构造和 exec 环境传递，**不通过 argv、命令文本、临时文件、ACP 帧或 stdin prompt**。可信 ACP 进程会继承并传递该 env，必须纳入保密 TCB，不能声称只有最末端 Pi 进程能看到 key。
6. launch 成功/失败、cancel、EOF、超时和关闭均清除宿主 SecretLease 引用；不刷新、不写回原生认证。禁止 core dump、调试 attach/export、环境 dump 和子进程继承无关句柄。Python/Node GC 和 OS swap 不提供可验证的内存零化，因此不宣称密码学级擦除；同 UID 宿主/管理员属于可信边界。若要求连受管进程环境也不得持 key，须另行设计凭据代理，不能偷偷改为落盘。

不得将真实 key、Authorization header、cookie、OAuth token 或原始环境复制进 `runtime/`、task 目录、日志、trace、checkpoint、release manifest/transaction 或 subprocess argv。错误只保留审查过的错误码、阶段与请求关联 ID；不得记录原始 HTTP request/header、异常 message 或原生 stderr。对外返回与事件 journal 也须经过 secret sentinel 防泄漏测试。环境只保留 [clean_environment](../pi_toolchain.py) L166–171 的基础项、已有 runtime/model locators、固定证书/出口 locators 和上述单个认证变量；排除 NODE_OPTIONS/NODE_PATH、PYTHONPATH、LD_*/DYLD_*、ambient proxy、SSH/cloud/actor/session 凭据。不存在供模型使用的 env-read 或认证文件工具。

## 4. 受信任快照、启动物与 OS 边界

### 4.1 快照与 P3-1 的两层证明

宿主必须显式提供 `snapshot_root` 和完整 commit，而不是寻找“最新”目录或信任可变 main。未来实施首先调用/复用 [verify_snapshot](../scripts/release_snapshot.py) L222–238 的只读校验：qualified、精确 root/commit、owner、封存 modes、完整 inventory、外部 interpreter/link 和 Node/npm 哈希。封存来自 `stage()` 的 archive/tree 对照与目标目录离线资格（L322–366），但本轮**不执行 stage/dry-run/activate/rollback**，不访问真实 controller 状态来“帮助设计”。

选中快照后，以其代码拥有的 registry 执行 `prepare_toolchain` 和 `load_launch`；release manifest 不代替 Pi/npm 包与派生 adapter 校验。生产同样调用 `derive_directory(role='coordinator')`、`write_launch` 的固定启动绑定及 `write_exclusions`。确有生产 launcher 扩展时，只能在这些共用校验之后选择代码拥有的生产入口，并把该入口/guard 纳入 host source 验证与快照 inventory；禁止自行跳过复验来传环境。

父 ACP 与子 Pi 使用同一已验证 Node/Pi，Node ≥24、Pi 1.5.2、pi-acp 0.0.34 和当前 coordinator SHA 全部保持。后端输出字节不变是默认验收；若新增 guard/transport hook 必须改变兼容变换，要另行评审命名变换与 registry 新指纹，不能在实现时静默修改。P3-1 package digest 只覆盖 Pi 自身文件，不包括外部依赖；依赖完整性另由 lock、快照 inventory 和审查证明。

`runtime/trusted`、`runtime/adapter` 仍为新建私有 0700 目录/0600 文件、原子禁止覆盖发布；模型进程的 OS 策略禁止写入、重命名、删除、symlink 绕路及替换 runtime 根。快照和可执行文件同样只读且在 project 外。不 chmod 挂载盘或放宽可信路径以通过准入；例如 `/Volumes/data` 的组可写祖先会被 P3-1 拒绝，设计 worktree 并不是可直接上线的可信安装根。owner 能主动改 seal 的事实，与模型受 seatbelt 约束的写保护必须分别验收。

### 4.2 网络、进程和工具权限

生产不能沿用 Local 的 `(allow default)` 当作文件保密边界：现有 profile 主要限制写和网络，原生 auth 对进程读取的隔离需要生产独立补齐。生产 macOS seatbelt 默认拒绝，逐项允许已验证解释器/动态库/证书、快照只读、runtime 必需 scratch、显式项目文件及 guard 所需的绑定工作流元数据；禁止读取真实 home、原生 auth、无关项目和其他工作流。策略和模型 file guard 均需拒绝敏感路径与 symlink/硬链接绕路，不能只靠提示词。

| 边界 | 拟议强制策略 | 不可实现/未资格时 |
| --- | --- | --- |
| Provider 出网 | 仅代码审查表中单个 HTTPS 主机/端口；宿主验证 DNS 结果，拒绝 loopback/private/link-local/metadata/保留地址、无限 CIDR 和动态 URL；显式企业私网 Provider 需另行策略评审 | 不启动；不放开 `network-outbound` 全网 |
| OS 与域名策略协作 | 初版通过宿主拥有的受限 CONNECT 出口，Pi seatbelt 仅允许该出口地址/端口，出口仅允许固定 Provider FQDN:443 与已验证地址集合；端到端 TLS/SNI/主机名验证，无中间人解密/凭据日志 | 必须离线证明 pinned Pi 的具体 provider transport 确实使用该出口；仅设置 HTTPS_PROXY 不算生效证明，不支持则拒绝此组合 |
| DNS/重定向 | 仅可信出口进程经批准 resolver 解析，Pi 无任意 DNS/direct TCP/UDP；连接前后验证地址，地址漂移需新准入；非允许域名 CONNECT、跨源 redirect、代理绕过和 TLS 错误均拒绝 | 不通过宽泛 DNS/域名通配、`NO_PROXY=*` 或关闭证书验证补救；IP seatbelt 本身不被描述为 FQDN/L7 校验 |
| 工具 | 只允许当前 guard 的 read；若另行选择 workspace-write 才允许 read/edit/write，仍拒绝 `.git/.codex/.ato/.pi/.aws/.ssh/.env` 与 runtime/snapshot；无 Bash/Shell、Web/fetch、MCP、glob/tree、slash commands、自动扩展/skills | 任意未知 tool/ACP host callback fail-closed，不能经 request_permission 提权 |
| 进程 | 宿主拥有单一新进程组；只允许代码拥有的固定解释器/launcher/ACP/Pi 链，关闭任意进程执行能力 | P3-1 launcher 内的固定 `/bin/sh` 只用于受验证脚本，不能转化成用户 Shell 工具；执行限制无法证明则不启用生产 |
| 文件与环境 | 最小读集合、scratch 写集合与 `write_exclusions` 叠加；无真实 home，单个 key env，只读原生凭据解析在 sandbox 外的可信宿主完成 | 不放宽到整个 HOME/磁盘读取；不将宿主 key 文件挂入 Pi |
| 平台 | 首版仅在实际通过 seatbelt 行为门禁的 macOS；Linux 策略需独立 namespace/bubblewrap 等资格 | 无 sandbox-exec、策略不可编译或测试被平台限制时不降级成无沙箱；Linux 暂报 unsupported |

`--offline` 等 CLI 参数保留用于阻止包/更新等环境发现，不能作为“不会调用模型”的证明；生产网络许可仍由上述 OS/出口边界决定。受管 Provider 请求是宿主授权的模型动作，不会使 Shell/Web/MCP 成为模型工具。出口限域不能保证 Provider 不计费，也不能撤回已经发送的数据。

逐请求控制不能只依赖 CONNECT：一个 TLS 隧道可承载多个请求，透明出口看不到加密后的 HTTP 重试或 token 参数。因此拟增加受信任的 `pi_production_transport.mjs`，通过固定 Pi 版本已离线验证的 provider 注册/传输接口接管**所选生产 provider**，每次实际发送前调用同一 guard 的生产 `authorizeAction`，申请成本预占、限制输出与请求次数，并强制受限 CONNECT、证书验证、禁止重定向和有界连接/读超时。它不是模型可调用的 Web 工具，不接管 Local 的 provider，也不新增第二套 ACP 源码变换。必须把原生 provider 的内层自动重试禁用或全部纳入这个路径；没有已证明的固定版本接入点时，设计要求拒绝该 provider，而不是只在外层 prompt 包装后宣称覆盖完整。这个传输扩展与 guard 都须纳入快照/host source 指纹及 OS 只读保护。

## 5. 每次动作前守卫、预算与断连收尾

### 5.1 统一准入与持续检查

保留 [guard](../pi-coordinator-guard.mjs) L18–41 作为每次文件工具调用的必经 native hook；为生产增加**同一 guard 的受限分支**，在既有 turn/STOP/contract_hash/generation/origin/events 复验之后，向宿主私有控制通道请求一次短时 `authorize_action` 判定。请求只含精确 host/turn/workflow/profile 身份、动作类型和参数摘要；不能携带 key、任意命令或要求扩大权限。socket 认证使用现有 host 身份边界，**不借用 Provider key**；一次判定只用于本次动作，不可缓存后用于另一个动作。

宿主的生产 fence 在绑定的执行租约内调用原 workflow owner 的 guard/`budget.poll`，按最新账本重算准入，不能由 JavaScript 复刻预算算法或靠静态 deadline 文件自行延长。guard 收到拒绝、通道断开、超时或错误形状即 block；通过后还需执行原路径判定。检查与实际文件效果不能声称跨进程原子事务；host 失效即停止 turn/杀自有进程组，并用竞态测试确定边界。

host actions（create/bind/send/Provider 请求准入、外部 completion 的接受）也先走相同 production fence；模型不可通过 `_control` 新 action 获得运行命令/审批能力。create 只消费显式 creation permit 并初始化无模型请求的新身份，随后须绑定冻结 origin；生产效果动作必须有绝对 workflow 路径、精确 origin/contract/generation 和有效租约，拒绝现有 `workflow_guard` L113–114 对 synthetic 相对 ID 的提前返回路径。绑定前不得发送 prompt、文件工具或 Provider 请求。cancel/close/撤销许可始终允许执行，不能因 STOP 或预算已过而阻止清理。Provider 自动重试如在 Pi 内发生，必须经生产 transport 逐次申请请求准入并复检 fence/费用；不得只有最外层 prompt 或 CONNECT 检查一次。若固定版本接口无法做到逐次请求准入，生产资格失败，不能以透明自动重试绕过检查。

预算保持双层：

- 总任务时间政策仍由现有 `budget.time_enforced()` 和账本决定；advisory 不因累计时长到点停工，enforced 到软 deadline 必须先执行 W1 的进展复检，再判断是否在既定 hard/extension 内允许继续。guard 不增加预算、不清历史、不把 heartbeat 当独立 passing evidence。
- 单次云端费用与硬超时是另外的授权护栏，**advisory 也不能关闭**。以宿主 monotonic clock 管理请求/turn 的绝对期限；预占 provider 请求数、输入/输出 token 成本和重试总成本。无法计算成本上界、计费表不明、usage 缺失或剩余额不足时停止新调用并保留不确定支出，不重置总额度。

现有 `workflow_guard` 无预算判断、Node guard 无 budget 字段，因此拟在 Pi 适配边缘增加 `pi_production_fence.py`，接收宿主注入的通用 owner-guard 回调；它不得 import coordinator/registry/路由来反向建立循环，也不能新建独立预算账本替代总任务账本。未来实现须证明这条 owner 回调对原 workflow 账本实际生效。

### 5.2 慢响应、取消与退出语义

W1 `a2c84a6` 的 [Runner.command](../runner.py) L141–154 使用有限 `Popen.wait`，子进程一退出即唤醒，仍在≤150ms 的轮询边界检查守卫；[budget.poll](../budget.py) L390–417 修复跨软 deadline 的过期判定。生产 async host 要实现等价的事件等待：同时等待 owned child exit、ACP response、STOP/fence 变化和 monotonic hard deadline，守卫检查间隔目标≤150ms（宿主正常调度下），不能只等网络超时、仅等 stdout 或固定长 sleep。慢网络不算已授权 external_wait，也不暂停 STOP/成本/硬超时。

| 时点/事件 | 收尾与持久语义 |
| --- | --- |
| 尚未写 prompt/发请求就准入失败 | 记明确 no-native-effect，不发网络，不启动另一个 profile |
| 已持久化 dispatching、写入状态未知、HTTP 已发而未获确认 | 保留 `delivery_unknown`/未决费用与原 message/turn ID；断连不是安全重试依据 |
| STOP、合约/代次变化、超时、代理断连、宿主退出 | 先撤销新工具/Provider 准入，取消请求并发送有界 ACP cancel；无论 pipe 是否损坏都进入 owned-group cleanup |
| TERM/KILL 清理 | 沿用 [cancel/close](../pi_session_coordinator.py) L498–550 的 TERM→2s→KILL→2s 思路，但生产必须给 cancel write/drain 本身设上限，并核实持有的进程对象/启动身份及 PGID；不从磁盘旧 PID 杀进程 |
| 直接子进程已退出，但后代/出口仍在 | 立即唤醒并核查自有组/出口的退出，不依赖 `proc.returncode is None` 才清理；当前 close 的该条件是生产需补的缺口。禁止扫杀他人进程，无法证明清理则 `cleanup_blocked`、不自动重建 |
| 迟到模型响应/成功 stop | 必须在接受前复核相同 profile/turn/fence；不能覆盖 cancel、unknown、native error 或据此完成 workflow；框架 receipt 与用户/业务验收分开 |

进程停止不等于云端已取消或零费用；不得把未知请求当作未发送。保留原合约、累计历史、STOP、尝试、身份与账本；不自动 restart、reconstruct、重放 mailbox、恢复旧 checkpoint 或清掉未决记录。调用前申请取消的无副作用承诺只覆盖本地准入，不声称能回滚外部费用。

## 6. 能力声明与 W2-2 分界

生产 kind 的 `session.create/verify/transport/observe/activity/cancel` 只有通过相应离线门禁后才能登记实现证据；`actor.authenticate` 仅沿用已有 session_host 与独立 origin 角色，不赋予用户决定权。provenance 区分 `profile_enabled`、`offline_qualified` 与 `cloud_model_qualified`；开启配置或原生 auth 存在不等于资格通过。

**`session.load_resume = unsupported` 保持不变。** 上游 ACP `loadSession=true`、支持 `--session`、进程重启后找到 session 文件都不能升级声明；生产不得调用 load/自动采用旧 native identity。W2-2 将单独覆盖原身份验证、旧进程终止、代次/幂等、未知效果和 checkpoint 的恢复设计。现有显式 reconstruction 也不能充当本次 opt-in 或生产自动恢复捷径；更换 profile 必须新身份与真实授权，保留旧 lineage。

## 7. 分级验收

### 7.1 离线必过门禁（未来实现准入）

所有测试用临时 ATO/native HOME、合成 provider/凭据、loopback fixture 和 owned children；不能读取实际 auth 内容、使用实际 key 或触发真实云端。本地 stub 可以模拟任意 Provider 事件，但不能记为云端资格。要求 0 失败/错误/跳过；平台沙箱不可用应报告不具备资格，不能 skip 凑通过。

| ID | 必须验证的正反例与实证 |
| --- | --- |
| O1 隔离 | 原 Local 回环+dummy 全部正反例继续通过；生产缺开关、错 kind/profile、未知字段/重复键/类型、task 注入、凭据缺失、旧身份/旧任务采用全部拒绝；拒绝后无连接/无 Local fallback |
| O2 凭据 | 合成 auth/env 两种显式模式；错误权限、symlink/hardlink/路径越界、超大文件、空 key、未知 native 格式、OAuth/`!command` 拒绝；不写原 auth。多 provider 不混选、无环境继承；验证 pinned Pi 的 env 引用行为而非仅检查生成文本 |
| O3 防泄漏 | 使用唯一 synthetic secret sentinel，在成功/异常/取消/EOF/超时/费用不足路径递归扫描 runtime、任务、stdout/stderr、trace、checkpoint、provenance 和 argv；包括异常包装/JSON 格式、依赖目录别名与 logging pathname 规范化；最小 env 仅有准许的一项真实形式的合成认证变量，Launch/state 不含秘密 |
| O4 工具链 | 复跑 P3-1 path/owner/版本/hash、父子相同 Node/Pi、坏 source/backend/capabilities/output、并发 no-replace 派生与不修改 node_modules；快照被移动、文件/工具/链接/mode 被改均拒绝 |
| O5 OS 沙箱 | **真实 sandbox-exec 行为测试**：允许 scratch/选中 read，拒绝 auth/HOME/无关任务读取，拒绝 snapshot/trusted/adapter/runtime 根写与 rename/alias；拒绝 Shell/Web/MCP/扩展；仅检查策略字符串不算通过 |
| O6 出网 | 全程本地受控 TLS/CONNECT/DNS fixture：允许审查目标，拒绝另端口/域名、直连绕代理、IPv4/IPv6 private/metadata/rebinding、跨源 redirect、坏 TLS/证书及环境 proxy 注入；证明 pinned Pi 的生产传输扩展实际接管请求，包含同隧道多请求与原生内层重试逐次准入，不允许真实 Internet fallback |
| O7 每次守卫 | Node guard 实际执行：tool/action 前插入 STOP、改 contract_hash/generation/origin/events/profile、断 fence socket 均拦截；Provider 重试准入逐次计数；cancel/close 在所有拒绝状态仍能收尾；未授权 ACP callback 无效果 |
| O8 W1/生命周期 | deadline 到边界有新独立进展时重检、无进展/硬上限拒绝、advisory不误停；连接/首帧/stream 卡住、先 EOF 后孙进程、断 control pipe、TERM 不退等真实自有子进程场景；确认退出唤醒/有界取消、组清理或 cleanup_blocked；无自动 replay |
| O9 能力/费用 | load_resume 始终 unsupported；不能持 fixture provenance 报生产成功；费用整数最小货币单位计算、输入/输出上界、自动重试累计、usage 未知不释放预占；单次硬超时与任务政策独立 |
| O10 回归/架构 | 全量 `scripts/run_tests.py`、下节结构/核心 AST/元数据/分发门禁，保留原 assert 与预算/STOP 条件；互相依赖≤8对且为历史基线子集；文档链接和索引通过 |

### 7.2 真实 Provider 冒烟（需单独人工授权，当前禁止执行）

授权必须指定 Provider/模型/账户、确切 HTTPS endpoint、credential 引用、隔离项目、执行机器/出口策略、有效窗口、最大请求数以及费用/硬超时；不能用“设计通过”或“离线全绿”替代。建议首次请求 **总费用≤USD 0.05、绝对硬超时60秒、输入≤1024 tokens、输出≤128 tokens、仅1次请求、禁自动重试、只读或无工具**。以 Provider 可验证价格/全量 prompt token 上界预占，参数并非当前支出授权。Provider 不提供可证明的请求上界或传输层无法限制隐式重试时，先拒绝冒烟。

- 独立临时项目仅放合成文本，无真实仓库/用户材料；无 Shell/Web/MCP、无业务 endpoints、无 Git/邮件/发布调用。费用请求本身是唯一预期外部效果。
- 先在离线重复验证同一出口/guard/取消策略，再申请一次真实 completion；仅记录脱敏 request ID、状态、token/费用、时延、清理回执与 profile/toolchain 标识，不保存凭据或原始 HTTP。
- STOP 急停是同一授权清单内的独立场景；不得为测 STOP 暗中多发第二次请求。若需额外真实请求，授权必须明确合计次数与费用；否则用本地慢网络 fixture 验急停。
- 达费用/硬超时即撤销新准入并按第5节清理；未知收费/结果进入未决，不自动重试。清理本次自有临时进程/出口/句柄，移除启用许可，恢复原默认本地选择，原生 auth/其他任务状态不变；保留审计而非回滚账本。已产生云端费用与已发送数据不能撤回。
- 冒烟成功仅证明该 provider/model/版本组合当次通过；不升级 native resume，不授权安装、入口/controller 切换、旧任务接管或持续云端运行。部署仍须另行按发布流程评审。

## 8. 拟改动模块与依赖门禁

下表是后续实现的候选清单，本轮**不修改**这些文件；如实施范围超出须先更新评审。新 Python 模块还需在 `pyproject.toml`/分发清单登记。

| 模块 | 拟议职责/方向 | 依赖约束 |
| --- | --- | --- |
| 新 `pi_production_profile.py` | 可信 opt-in 校验、非敏感配置/身份、生产 seatbelt 组装 | 仅依赖标准库、toolchain、compat 和下列叶模块；不回调 coordinator import |
| 新 `pi_production_credentials.py` | 原生 auth 只读解析、单变量 SecretLease | 标准库；Pi 格式仅在此，禁止业务/核心模块 import vendor parser |
| 新 `pi_production_egress.py` | 本次 owned CONNECT 出口与域名/IP限制、请求上限/撤销 | 标准库与注入的通用判定回调；不依赖 coordinator/profile，禁止成为通用开放代理 |
| 新 `pi_production_transport.mjs` | 只接管所选生产 provider；逐请求调用 guard/成本预占，强制出口/TLS/无重定向/有限重试 | vendor 传输接入仅在此受信任 adapter；不改 Local、不绕过固定 compat 后端；依赖与输出指纹须审查 |
| 新 `pi_production_fence.py` | 将 owner guard、profile、单次费用/硬超时合并判定 | 标准库+注入 owner-guard；不 import Pi host/核心路由，无第二份总预算算法 |
| 新 `pi_production_exec.py` | 固定生产 native 入口，最终最小 key env，禁 core dump | 复用 pi_toolchain 校验；不读取任务/模型指定的 auth 或执行命令 |
| `pi_session_coordinator.py` | 精确生产类型/kind、秘密内存启动分支、fence 控制与持续监视、退出证明 | 仅 adapter 层单向依赖新 profile/fence；不改八核心来识别 Pi |
| `pi-coordinator-guard.mjs` | 每工具复验，生产分支请求一次 budget/action fence | Local 的原分支保留；无秘密、无 Shell/Web、无模型触达管理接口 |
| `pi_toolchain.py` | 如需固定生产 launcher，将新来源纳入同等 host source/launch复验 | 保持标准库叶模块，不 import profile/coordinator/新 credentials；Local 环境函数保持原义 |
| `pi_acp_compat.py`、`compatibility.json` | 默认仅复用，不改后端和派生输出 | 若 transport 资格需要额外变换，须先评审，不在实现时临时 patch |
| `pyproject.toml`、分发与相关 tests/docs | 新模块登记、合成 fixture 与资格证据 | 不安装凭据、不创建运行态、不改变已有测试断言换取通过 |

依赖必须保持 `coordinator → production profile → 叶模块/toolchain/compat` 的方向；回调以通用 callable 注入，叶模块不能反向 import host。注意现有 compat→toolchain，故 toolchain 不能再 import compat 或 production profile。类型提示/延迟 import 也计入架构图，不能以函数内 import 藏循环。

门禁原文： [check_architecture.py](../scripts/check_architecture.py) L11–39 对根模块/CLI AST 收集含延迟 import 的依赖，L42–54 要求 runtime 双向依赖≤8；[结构测试](../tests/test_architecture_structure.py) L37–46 进一步要求≤8且是 [历史31对基线](../tests/fixtures/architecture-baseline.json) 的子集。不得删改 baseline 或放宽计数。

八核心模块为 `control.py`、`recovery.py`、`interaction.py`、`delegation.py`、`workflow.py`、`session_router.py`、`message_bus.py`、`transport.py`。本设计不需要修改它们；其 AST 不得新增 Pi/vendor imports、原生 auth/event 私有解析器或 subprocess Pi 识别分支。现有 [核心 AST 测试](../tests/test_codex_session_adapter.py) L354–361 主要检查 Codex vendor imports，不能冒称已经自动禁止全部 Pi parser；未来需在 adapter 边界测试补齐通用 vendor 禁入证明，并保持八核心 AST 与本基线一致。

## 9. 本轮文档验收与评审停点

在本独立 worktree 执行用户指定命令：

```sh
.venv/bin/python scripts/run_tests.py
.venv/bin/python scripts/check_architecture.py
```

第一条由 [run_tests.py](../scripts/run_tests.py) L33–47 创建临时 ATO/Codex homes、清除会话令牌并拒绝 skipped；其中须含 `test_documentation.py`、`test_architecture_structure.py`、`test_repository_metadata.py` 和全量749+用例。第二条只读输出架构图；还需从结构测试确认旧基线子集，不能只看脚本 exit 0。

本轮验收结果由实际日志另行汇报，不能把本文未来 O1–O10 或冒烟清单标成已完成。完成文档和这两项离线验证后停下等待用户评审；不开始实现、不 merge/push、不操作入口或 controller、不调用真实云端。
