# Prismloop VePhone Client Gateway

这是 Harness 私有 sidecar，不是 Agent 可调用的公开接口。它用官方 Web SDK 在受控浏览器进程中连接
一个云手机 Pod，并固定一个 Canvas 视频 track 与一个 MediaStream 音频 track。Harness 的 A/B 切换只
通过 `/v1/private/*-frames` 更新这两个 track 的内容，不重连 SDK 会话。

启动前，父服务必须从 `VolcClientSessionBroker` 获取短期 STS token，并仅在子进程内存环境中提供
`PRISMLOOP_VEPHONE_SESSION_BOOTSTRAP` JSON。它包含账号、用户、Product、Pod 和短期 token；不得
写入仓库、SQLite、TOS、日志或 shell 历史。没有该 bootstrap 时，`GET /health` 返回
`capability_unavailable`。

该进程仅监听 `127.0.0.1:8791`。接口是 Harness 内部 transport，不能暴露给 Agent 或公网。
Harness 通过 `LoopbackClientGatewayTransport` 调用它；请求只有标准化 profile、帧序号、PTS 和媒体
字节，绝不含云机或鉴权字段。
