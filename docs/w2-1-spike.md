# W2-1 前置 Spike：锁定 Pi 1.5.2

基于 `bf6037b`，合入已批准设计 `60abce6`。这是离线资格记录，非云端或运行态验收。两项 Spike 在编写本实现 worktree 的生产模块前完成；没有修改 `pi-acp` 输入、变换、输出或 registry。操作者的独立 build-logs 不包含在源代码分发中。

## 固定输入

复用已缓存的 npm `@earendil-works/pi-coding-agent@1.5.2` 发布 tarball。验证 [registry](../integrations/pi-acp/compatibility.json) 的 SHA512 integrity、CLI entry SHA256 `e79626f2dd6f94aa45d30f3fa63cd84319a6eefcd150b353cfaf274366926774` 和完整自身文件 package digest `79c28471288d1724b75c72b6f873cc04449437f6e6a51187d13f234aa68dc935`；不是从正在开发的 Pi checkout 推断能力。Node 为24.22.1。仅从 npm 本地 cache 离线补入该发布指定的 jiti2.7.0。

## A：环境引用没有字面值回退

真实 `dist/core/resolve-config-value.js` 在禁网 seatbelt 中执行：`$ATO_SPIKE_KEY`、`${ATO_SPIKE_KEY}` 缺失时返回 undefined，严格解析接口抛错；存在时得到合成值。发布源码 L83–95、L117–128、L199–217 与行为一致。

再导入真实 `dist/bundle/index.js`，使用 `ModelRuntime.create` 读取临时 `models.json`，凭据为 `${ATO_SPIKE_KEY}`：`getAuth(model)` 缺失时不可用，存在时得到 synthetic 值。原生 auth/settings 未读取。生产只准生成这个精确引用，不接受 bare name、转义或命令表达式。

## B：真实扩展、CONNECT 与禁重试

真实发布 CLI 显式载入唯一 synthetic extension，隔离 native HOME，禁用 skills/MCP/context/自动扩展。`pi.registerProvider` 的 `streamSimple` 确实接管所选 provider（发布 `dist/core/extensions/types.d.ts` L1459–1478）；不 patch CLI 或 ACP。

扩展每次发送前实际调用 synthetic `authorizeAction`，使用自己的单请求 socket/TLS 客户端，没有默认 SDK fetch/内层重试；`retry.enabled=false` 禁止外层 Agent 重试。Provider 为本地 TLS fixture（临时自签证书），CONNECT只接受固定 FQDN:443；Node seatbelt 仅准连接代理端口，另一个本地端口直连实测 EPERM/EACCES。

| 场景 | custom handler / authorize | CONNECT / Provider请求 | 结果 |
| --- | --- | --- | --- |
| TLS成功 | 1 / 1 | 1 / 1 | CLI退出0，合成completion |
| Provider返回503 | 1 / 1 | 1 / 1 | CLI非零，无自动重试 |
| authorize拒绝 | 1 / 1 | 0 / 0 | CLI非零，无请求 |

这是接管点的最小实证，不代替O1–O10的完整资格或默认拒绝 Node 沙箱测试。CONNECT本身看不到TLS内的多个HTTP请求；必须通过自定义传输逐请求准入，不能仅设置HTTPS_PROXY。后续传输须保留payload/response hooks。

## ACP输出

对锁定原始SOURCE运行未修改的 `pi_acp_compat.derive`：worker SHA256仍为 `6dfbd37ca70ece07c4fe7242cf0c4389eafc7a6dc48102db43bb3f0c1b12d275`，coordinator仍为 `1f9eb67469862d835192f2ea86266ecf7a1d58eefd4dec4e77d5f35447790bc8`。两项前置门禁通过，可实施W2-1A；本记录不批准7.2冒烟、运行态切换、合main或push。
