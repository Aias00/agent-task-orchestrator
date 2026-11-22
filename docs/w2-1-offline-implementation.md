# W2-1 Production Pi 离线候选

实现基线为 `bf6037b`，带入已批准设计 `60abce6`。本候选只覆盖设计7.1节O1–O10；真实Provider、在线运行态、main合入及push均不在本轮执行范围内。原 [设计](w2-1-production-pi-design.md) 的源文件行号仍以其09c0f8f设计基线为准。

## 接入边界

- `ProductionPiProfile` 独立于原 `LocalPiProfile`；默认不会注册生产adapter，没有默认已批准的云端Provider。宿主必须显式提供严格配置、provider/model/endpoint/地址/成本审查表、有效许可判定和精确封存快照root+commit。
- 生产代码必须从选中快照执行并通过 `verify_snapshot`，随后仍走原 `prepare_toolchain`、兼容后端派生、`write_launch`/`load_launch`。`runtime/trusted`、`runtime/adapter` 与runtime根替换继续受OS保护。新launcher来源也纳入指纹；旧Local调用路径不变。
- `PiSessionAdapter(kind='pi_acp_production')` 只能由可信宿主显式注册。创建新host需要精确生产profile和现有workflow owner；不能带旧bindings构造，也不能消费Local的prepared provisioning。新native identity创建后才冻结origin并绑定绝对workflow。owner必须具有既有accounting segment；每次action调用其原guard，不复制或重置W1预算算法。
- native auth只读支持被显式选择的静态 `api_key`；env模式只读取一个指定变量。真实Provider key只在短时SecretLease和受管进程最小环境中，模型配置只含 `${ATO_PI_PROVIDER_KEY}`；无原生home拷贝、OAuth刷新、命令凭据、key argv或环境dump。
- 默认拒绝seatbelt只放行固定启动链/代码/系统库、必要项目读取或授权workspace-write、runtime scratch及固定出口/控制socket。Pi包仅放行已验证根的读取及固定入口的可执行性复检；workflow仅放行绑定目录读取。拒绝原生auth、无关目录、敏感路径和`.env.*`。固定`/bin/sh`及macOS的`/bin/bash`分派仅服务受验证launcher，不提供Shell模型工具；无Web/MCP工具。

## 每次请求和停止

guard在原turn/STOP/合约/代次/指纹检查之外，经私有control socket请求宿主预算许可；Provider动作同样逐请求检查。其后使用一次性CONNECT grant、固定FQDN和重新验证的公共IP集合。传输自己执行一次HTTPS/SSE请求，不调用带隐藏重试的SDK，不跟随重定向，不使用ambient proxy，不关闭TLS验证。DNS解析在有界、无凭据的自有子进程中进行。

每请求先持久化完整的保守成本预占，再发CONNECT grant。原生usage缺失、断连、取消或未知结果不退款。单次monotonic硬期限独立于总任务advisory政策。v1仅支持被审查的文本/OpenAI-compatible路径；序列化字节加framing reserve保守限流，大上下文在发网前拒绝。价格、token和次数上界来自宿主审查，不是模型参数。

宿主持续监视STOP、代次、owner guard和子进程退出；无需等待下一次工具调用。显式cancel的ACP写入上限为250ms，写管道卡住仍进入关闭。生产supervisor在ACP子进程退出后保留自有组anchor，并用私有fd唤醒宿主，即使孙进程仍持有stdout。关闭先撤销准入/出口，再对已证明的组cohort终止、必要时KILL并复核；不能证明身份或清理则 `cleanup_blocked`，不扫杀未知进程。迟到响应不能把取消、unknown或错误改成workflow完成。

`session.load_resume`仍为unsupported；生产checkpoint reconstruction同样拒绝，留待W2-2。没有隐式重启、重放、Local回退或旧任务接管。

## 离线证据映射

| 门禁 | 可复验入口 |
| --- | --- |
| 前置Spike | [锁定包记录](w2-1-spike.md)：真实Pi环境引用、扩展接管、CONNECT、503无重试；两个ACP输出SHA未变 |
| O1/O2/O3/O9 | `tests/test_pi_production_profile.py`：显式opt-in、严格schema、私有auth/env、序列化防泄漏、代次/期限/整数成本预占 |
| O4 | `tests/test_pi_production_transport.py` 与既有toolchain/compat/release_snapshot测试：launcher来源篡改、包/输出hash、封存清单/工具与链接变更 |
| O5 | `test_default_deny_policy_with_real_node_not_python_fixture`：实际Node24进程验证允许/拒绝读写、保护目录及根替换、敏感auth和出网 |
| O6 | 真实Node、本地TLS/CONNECT、证书失败、503/redirect、DNS变更、无效/复用grant、慢响应和SSE/工具终态负例 |
| O7/O8 | `tests/test_pi_production_coordinator.py`：实际Node guard+默认拒绝seatbelt+Unix IPC、STOP/代次/origin/预算、期限、退出唤醒、忽略TERM的孙进程清理、无接管/重放 |
| W1 | 既有budget/runner全量回归与新host owner委托测试；deadline进展判断继续属于原owner |
| 日志软链修复 | `tests/test_acp_stderr.py::test_sdk_filter_normalizes_symlink_venv_record_pathname`：真实SDK目录别名经realpath规范化后仍脱敏 |
| O10 | 全量runner、py_compile、architecture-baseline子集与八核心冻结AST/字节检查；新增模块同步进入pyproject和候选分发清单 |

另用真实锁定Pi执行新增传输模块、真实guard/control及本地TLS完成一次合成请求。测试CA仅在这个明确的native协议fixture中注入，生产环境构造器不透传它；此证据不等于完整封存启动链或真实云端资格。操作者日志与原始验收材料保留在Git之外。

复验使用 `.venv/bin/python scripts/run_tests.py`、全量Python编译及 `.venv/bin/python scripts/check_architecture.py`，必须0失败/错误/跳过、双向依赖≤8且为旧基线子集。八核心模块的源码/AST不得改变。实现提交保持W2-1A/B/C分离，最终状态与实测计数见随交付报告。

## 明确尚未执行

没有执行设计7.2真实Provider冒烟，没有读取/验证本机真实Provider key，没有安装、切换入口、操作在线controller或迁移任务，没有merge/push。批准设计与离线实现并不批准这些动作；后续只等待用户评审。
