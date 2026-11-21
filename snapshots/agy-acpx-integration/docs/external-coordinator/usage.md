# AGY 通过 ATO 管理 Codex（中文使用说明）

本目录的 CLI 实现属于候选版本。`ato-control` 的实际安装、原生权限验证及真实
Codex/AGY demo 由 origin 最终交付阶段完成；本阶段没有安装该入口，没有调用模型。
仓库内也提供 `bin/ato-control` 源码入口（可设置 ATO_PYTHON 指向可信 Python），
它只转到同一个受限 CLI，不包含人工 bootstrap。生产 worker 使用单层原生命名权限
配置和受限网关；宿主权限与真实模型验证仍由 origin 完成。下面的 Python 入口可直接
用于审查候选代码。

## 人工准备，不能交给 AGY 执行

选择全新的临时 Git 仓库，复制并审查 [demo-contract.json](demo-contract.json)。
该示例仅创建 greeting.py，不提交 Git，不联网、不部署；ATO 的命令检查独立导入函数
并验证返回值。实际合约可以不同，但 objective/scope/authorization、每个阶段的
提示词、所有 argv 检查和限制都必须先由操作者审查。模型不能更改已经授权的合约。

在可信终端设置绝对路径，以下变量只是路径，不是身份或授权：

```sh
export ATO_HOME=/绝对路径/私有目录/ato
export CODEX_HOME=/绝对路径/私有目录/native-locator
export ATO_ACPX_CONFIG=/绝对路径/人工配置的私有依赖路径.json
mkdir -m 700 -p "$ATO_HOME"
RUNTIME=/绝对路径/候选或已交付运行版本
REPO=/绝对路径/全新demo仓库
CONTRACT=/绝对路径/已审查demo-contract.json
ACTOR="$ATO_HOME/actors/agy-demo.json"
python3 "$RUNTIME/external_operator.py" --repo "$REPO" --contract "$CONTRACT" \
  --actor-file "$ACTOR" --source '实际用户授权的来源、范围与日期'
```

REPO 必须是 Git 仓库的精确根目录；先由人工在空目录中运行 `git init`。
ATO_HOME 和 CODEX_HOME 必须显式指定、互相独立，并在仓库之外；禁止指向全局
`~/.codex`。CODEX_HOME 这里只隔离 ATO 的兼容身份命名空间，不会复制或读取原生登录。
acpx_codex 使用人工私有配置指定的精确依赖路径和原生 home；版本/hash/权限策略
由代码固定，任务合约不能覆盖。配置格式见 [acpx-worker.md](acpx-worker.md)。

bootstrap 创建 local_cli origin、保留在 ATO_HOME 内的人工 root 凭据，以及仅供 AGY
使用的精确仓库/合约 actor 文件（0600）。root 文件不传给 AGY、不放进仓库，也不放进
worker 提示词。重复 bootstrap 不能当成重试：同一个请求已经绑定旧 origin 时，应检查
原绑定；不要创建第二个任务绕过历史或 STOP。

这是同一 OS 用户的本地信任边界。文件证明防止把环境变量、会话 ID 或模型文字当成
身份；它不能隔离有意读取该用户所有私有文件的恶意进程。必须由宿主执行环境限制 AGY
和编码 worker 的文件访问。人工 bootstrap 程序不属于模型工具列表。

## 可复制给 AGY 的指令

交付后把下面的 Python 入口替换成 origin 核实过的 `ato-control`，参数完全相同。
不要声称当前候选已存在全局入口。只把受限 CLI 和 ACTOR 定位文件提供给 AGY。

```text
你通过 ATO 管理这次已授权任务。仅操作人工提供的精确仓库和合约。
先阅读合约，说明阶段与独立验收；不能修改合约、授予权限或代表用户验收。
可以整理授权范围内的计划；只有人工审查并绑定的合约能提交，不能自动换成另一个计划。
执行以下受限 CLI。所有命令在人工配置的私有 ATO_HOME/CODEX_HOME 下运行：

python3 RUNTIME/external_coordinator.py --actor-file ACTOR init
python3 RUNTIME/external_coordinator.py --actor-file ACTOR submit --contract CONTRACT
python3 RUNTIME/external_coordinator.py --actor-file ACTOR continue
python3 RUNTIME/external_coordinator.py --actor-file ACTOR status
python3 RUNTIME/external_coordinator.py --actor-file ACTOR events --cursor 0 --limit 50
python3 RUNTIME/external_coordinator.py --actor-file ACTOR result

需要停止时：
python3 RUNTIME/external_coordinator.py --actor-file ACTOR stop

submit 返回 workflow 后，可在后续命令增加 --workflow 该精确路径。
events 返回字节 cursor，下一次按需查询使用该 cursor；不要创建后台轮询器。
continue 是前台有界执行，应保留进程直到结束。可用 submit --run 合并首次创建和执行；
重复 submit --run 只返回原任务，不会再执行。不要因客户端超时重发提示。
报告总任务 status、criteria、证据位置和 accepted，区分阶段通过与总体验收。
若 STOP、权限/依赖缺失、未知投递或人工验收阻塞，报告原任务事实并交回人工。
不要调用 acpx CLI 作为任务管理器；不要调用 bootstrap、runner 的人工命令、hub
重绑定、授权/acknowledge 接口或直接更改 .codex/私有 ATO 状态。
```

## 结果与续办

`submit` 对规范化合约和精确仓库生成稳定指纹，创建一个 canonical ATO workflow。
`continue` 使用原 workflow/stage/attempt 历史和仓库独占锁，不启用 supervisor，
不修改 limits，不传 `--retry-failed`，不清除 STOP，不重置失败次数。
已有尝试的未完成 active stage 拒绝重新进入，避免既有通用适配器的重试路径重置历史；
这类失败交给可信操作者审查，模型没有解锁或重置操作。
只执行已审查 stages；用尽 stages 后剩余检查失败会 blocked，不调用隐含 planner。
任务状态为 running、waiting、interrupted 或有不明进程/ACP 投递时，外部入口拒绝续办。
acpx 结果待协调时也不能续办；必须先由可信操作者核对原始证据。

`status/events/result` 不记录用户已读、呈现、回答、批准或人工验收。`result.accepted=true`
仅表示 canonical workflow 已完成、所有 frozen criteria 已通过、没有 STOP/活动 owner；
它引用最近一次 ATO 验收，不是对之后文件变化的持续证明。需要重新验收可显式 continue，
它会重跑完整检查；旧 attempt 历史保留，失效结果不会被模型回复覆盖。
manual criterion 始终需要独立真实人工流程，AGY 没有 acknowledge 或回答能力。

若创建回执丢失，submit 返回 `admission=delivery_unknown`，禁止再创建或重新发送。
操作者应在私有 ATO request receipt、仓库 workflow/origin 与进程证据之间核对；当前模型
接口不提供解除未知状态或追加权限的命令。停止后应保留原任务和 STOP，任何新任务需重新
审查范围，不以新请求、改名或重新 bootstrap 绕过停止和尝试上限。

## 已交付后的短指令

仅在 origin 已核实并安装新增入口后，把实际 ACTOR/CONTRACT 路径填入以下指令交给 AGY：

```text
使用 ato-control 管理这份已审查合约，ATO 是任务与验收权威。
先运行 ato-control --actor-file ACTOR init，
再运行 ato-control --actor-file ACTOR submit --contract CONTRACT，
然后运行 ato-control --actor-file ACTOR continue。
最后用同一个 actor 查询 status、events、result，报告总任务状态、独立检查和证据路径。
需要停止时运行 ato-control --actor-file ACTOR stop。
不要直接管理 acpx，不修改合约或权限，不代表用户批准，不重发未知投递。
如果这个 workflow 已完成，只查询和报告原结果，不再创建或运行另一个 Codex 任务。
```

未完成真实宿主 gate 或入口 readback 时，仍是候选，不能把目标入口当作已安装事实。
原生登录失败、缺依赖、权限配置未确认或 phase 元数据不支持，都会失败并保留原任务。
不要用更宽权限、原生 fallback、新请求或删除 admission 来绕过。
