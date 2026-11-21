# AGY 通过 ATO 指挥 Codex

链路：AGY（主控）→ ATO（任务、权限、STOP、历史、独立验收）→ acpx/ACP（通信）→ Codex（编码）。

## 直接试用

交付核实后，在本机终端运行：

```sh
OPERATOR_HOME/.local/bin/ato-agy-demo
```

该命令建立全新隔离 Git 示例、精确授权合约和仅限此任务的凭据，打开一个新的 AGY 对话，自动附带操作说明。AGY 应调用 ATO 提交任务和执行 `continue`，让 Codex 创建 `greeting.py`；ATO 再独立运行 Python 断言。只有 `result.accepted=true` 才表示任务通过。

这个内置示例只有问候函数，便于先验证完整链路。它不使用当前工程、不连接旧 AGY 对话，也不自动提交或发布代码。

## 在已经打开的 AGY 里试

先由你在终端运行：

```sh
OPERATOR_HOME/.local/bin/ato-agy-demo --prepare-only
```

把终端最后打印的指令粘贴到你的 AGY 对话。AGY 按该次 `AGY-INSTRUCTIONS.txt` 操作即可。准备命令不调用模型，也不启动 Codex；这次任务在 AGY 运行 `submit` 后才进入 ATO。

每个示例目录都有受限 `control` 入口，可在终端或 AGY 中使用：

```sh
/本次试用目录/control status
/本次试用目录/control events --cursor 0 --limit 30
/本次试用目录/control result
/本次试用目录/control stop
```

`continue` 是前台有界执行。终端工具若返回仍在运行的 session，应等待原 session，不能再执行一次或重发任务。STOP、未知投递和已消耗失败尝试都保留，AGY 无权清除。

## 接实际项目

先确定目标 Git 根目录、允许改动的范围和独立检查命令，形成合约；由操作者审查后通过运行版 `external_operator.py` 绑定精确仓库/合约。再把受限 actor 和 `ato-control` 调用方式交给 AGY。完整格式与配置见安装回执所指运行版的 `docs/external-coordinator/usage.md`。

本版需要操作者准备授权合约；尚不是在 AGY 任意说一句话就能自动授权所有工程。模型可以规划和执行已经绑定的任务，不能自己增加权限、代表你批准发布或通过人工验收。

当前原生执行端实际资格范围为 macOS arm64、Codex ACP 1.1.5、Codex 0.144.6 和固定 acpx fork；其它 Agent 可沿同一适配边界扩展，不表示均已端到端验证。现有 ctask、hub 和旧任务保持原绑定；此入口独立试用。

## 验证与安装信息

`installation.json` 是本机新增入口的固定运行目录/依赖定位。`ato-agy-demo --check` 仅核对运行包，不启动任务或模型。最终实测范围、检查数和证据见本次 `delivery.json`；不要把通信完成或模型自述当作验收。
