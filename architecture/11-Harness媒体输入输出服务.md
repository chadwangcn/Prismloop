# Prismloop Media I/O Harness：媒体输入输出服务

> 文档版本：v2.0
> 更新日期：2026-08-29
> 状态：设计草案
> 范围：模拟媒体输入、采集媒体输出、保存原始证据与可选信号识别。

---

## 一、服务只做什么

Prismloop 是一个业务无关的 **Media I/O Harness 服务**。它不理解 PRD、Figma、页面、
APK、Git、测试用例、通过失败或 Bug。它只做四件事：

1. 从资产库取得受版本保护的图片、视频、音频或 PCM；
2. 按指定时序，把它们注入目标设备的摄像头、麦克风或其他媒体输入接口；
3. 采集目标设备的屏幕图像、屏幕视频、扬声器/系统音频和最小执行日志；
4. 将输入快照、原始输出和可选的机器识别结果写回资产库，并返回索引。

```text
任何 Agent / CI / 工具
       │ MediaRunRequest
       ▼
┌───────────────────────────────────────────────────────────┐
│                  Prismloop Media I/O Harness                │
│ sequence compiler · input staging · capability gate         │
│ injector · capture · optional recognizer · evidence index   │
└───────────┬───────────────────┬────────────────────────────┘
            │                   │
            ▼                   ▼
  Artifact Store adapter   Device adapter
  （首期：TOS）            （首期：Volcengine cloud phone）
```

调用方只看见 `environment_ref` 和 `artifact_ref`。云手机 Pod、ADB 地址、存储 bucket、
云凭证、临时 URL 和厂商 SDK 都是服务内部细节。

### 明确排除

下列事情不在本服务范围：

- 解释业务含义、PRD、Figma、UI 是否符合设计；
- 构建、签名、安装或提交 APK；
- 判断一个业务 Case 是否通过、是否提 Bug、是否继续开发；
- 管理 Git、任务计划、Dev Loop 或 QA 流程。

上层 Agent 可以把本服务返回的截图、音频、视频或识别信号用于这些目的，但该解释不会进入
Harness 的 API 或内部状态机。

---

## 二、稳定的输入与输出模型

### 2.1 输入：媒体源与时间序列

每个媒体源以不可变 `artifact_ref` 引用，必须包含 SHA-256 和 media type。服务不接受
“某个本地绝对路径”或可失效的签名 URL 作为可重放输入。

| 输入 kind | 资产格式示例 | 目标抽象 |
|---|---|---|
| `camera.image` | PNG / JPEG | 摄像头静态图源 |
| `camera.video` | MP4 | 摄像头视频源 |
| `microphone.pcm` | L16 PCM，采样率/声道明确 | 麦克风原始帧源 |
| `microphone.audio` | WAV / AAC | 服务转码后进入麦克风帧源 |

### 2.2 连续虚拟摄像头与视频序列热切换

`camera.video` 不是“一段文件对应一次相机启停”。服务为它建立一个长期存在的
`camera_stream`：虚拟摄像头只打开一次，`VideoSourceRouter` 在上游持续送帧，并能在指定
时间把解码源从一个视频 fixture 原子切换为另一个 fixture。

```text
video-A ──decode──┐
                  ├── VideoSourceRouter ──连续帧──► virtual camera session ──► device
video-B ──preload─┘           ▲
                               └── camera.stream.switch（不关闭 session）
```

这里的连续链路不是 RTMP 协议契约。Harness 的规范输出是带连续序号和 PTS 的标准化原始帧，
由 provider adapter 写入云手机的外部视频源接口。这样即使 A/B 是文件、图片序列或另一个实时
来源，切换语义也完全相同。未来可以增加 `RTMP/RTSP -> decode -> VideoSourceRouter` 的**可选实时
输入 adapter**，但 RTMP URL 不会成为云手机注入接口，也不会出现在首期的 `MediaRunRequest` 中。

若某个未来 provider 只能消费 RTMP，该 provider adapter 可在内部维持一条长期存在的 RTMP
输出会话；它仍必须把 Router 的连续帧转化为该会话，而不得以每段 fixture 各自重连推流来实现
`camera.stream.switch`。

每个 `camera_stream` 固定输出 profile（宽高、帧率、像素格式、方向）。所有待切换视频在
`input.stage` 阶段转码或补帧到同一 profile；切换时下一帧直接来自新 source，时间戳与序号
持续单调递增。短视频结束后按 stream policy 循环、保持最后一帧或切入指定 fallback，不能
令虚拟摄像头 EOS。

`camera.stream.switch` 的语义是 **切换内容而非切换摄像头输入通道**。它必须满足：

- virtual camera session ID 不变，`source_session_restarts = 0`；
- 输出帧序号连续、时间戳单调；
- 任意相邻输出帧间隔不超过请求声明的 `max_interframe_gap_ms`；
- 切换前后的原始输出视频与 `stream_receipt` 均写入资产库，供调用方自行判读。

当前 ADB `camera_file_preview` PoC 仅验证静态/循环文件视频，**不具备连续热切换的验证证据**。
本能力的实现和 PoC 必须使用保持单一外部源会话的客户端 SDK 帧流或 Pod 原始帧流；不得用
“关闭当前相机 → 改文件路径 → 再开启”冒充无断流切换。

#### 火山引擎三条注入路径的映射

| 官方说明 | Harness adapter | 是否适合连续 fixture 切换 | 使用边界 |
|---|---|---:|---|
| [Pod 原始数据注入](https://www.volcengine.com/docs/6394/1129851?lang=zh) | `PodRawFrameAdapter` | 是（待 PoC） | 服务端直接生产/代理原始音视频帧时使用 |
| [客户端内部源注入](https://www.volcengine.com/docs/6394/1182636?lang=zh) | `ClientInternalSourceAdapter` | 否 | 客户端真实摄像头/麦克风采集，不是资产库 fixture 播放器 |
| [客户端外部源注入](https://www.volcengine.com/docs/6394/1182637?lang=zh) | `ClientExternalFrameAdapter` | 是（首选，待 PoC） | Harness 从资产库解码后持续推送自定义帧 |

连续切换选择“客户端外部视频源”模式，而不是离线文件播放模式。服务的 provider adapter
只在 stream open 时设置一次 `setVideoSourceType(外部源)` 并发布本地视频；随后以稳定帧率
持续调用 `pushExternalVideoFrame`。`VideoSourceRouter` 在调用前替换帧内容，所以切换 A/B
视频不需要再次切换视频源类型。外部源与内部采集是不同采集模式；模式之间切换有采集启停
语义，故不能把它置于每次内容切换路径上。

音频同理：麦克风外部源是独立的 `setAudioSourceType` + 定时
`pushExternalAudioFrame` 会话，不能借用摄像头视频的 AAC 音轨。

#### 客户端外部源 Gateway（首个可验证执行路径）

截至当前 PoC，ACEP 管理 OpenAPI 已验证的能力是实例管理、安装、启动和截图；它没有经实测的
原始帧推送接口。相反，官方 Web/Android 客户端 SDK 暴露“设置外部源类型 + 启动外部视频/音频
track”的接口。因此 `ClientExternalFrameAdapter` 是首个可验证的连续注入路径，`PodRawFrameAdapter`
仍保留为等待官方服务端桥接资料后的候选实现。

```text
Media I/O 服务 ──私有标准化帧协议──► Client External Gateway ──官方 SDK session──► Pod
      ▲                                  （Web/原生客户端进程）
      │  不含 Pod ID、STS、Endpoint
VideoSourceRouter / AudioSourceRouter
```

Gateway 是服务受控子进程或同机私有 sidecar，而不是公开 API：它从凭证解析器/短期 token broker
取得短期 SDK 会话凭证，设置视频和音频 source type 各一次，并为每个 `camera_stream`/麦克风源
保留一个 MediaStreamTrack。`camera.stream.switch` 仅切换 gateway 上游帧内容，不能重启 track 或
SDK session。若未配置受控 token broker、账号标识或 SDK runtime，`camera.video.inject` 与
`microphone.pcm.inject` 必须保持 `unavailable`。

首期 token broker 使用服务自身 Keychain 凭证调用官方 `GetCallerIdentity` 获取账号标识，再以
`PRISMLOOP_VEPHONE_STS_ROLE_TRN` 调用 `AssumeRole` 签发 15 分钟至 12 小时的短期 token。角色
必须仅允许该服务 principal `sts:AssumeRole`，并限制到本 Harness 使用的云手机资源。浏览器/SDK
进程只接收内存中的短期 token；不得回退使用长期 AK/SK，也不得把 token 写入 env 模板、SQLite、
日志、TOS 或任何 MediaRun 结果。

`sequence` 是唯一的编排单位，只允许媒体和采集动作：

| action | 含义 |
|---|---|
| `input.stage` | 下载、校验、预处理媒体资产 |
| `input.start` / `input.stop` | 启动或停止指定输入源 |
| `camera.stream.open` / `camera.stream.close` | 打开或关闭一个长期虚拟摄像头会话 |
| `camera.stream.switch` | 在不断开虚拟摄像头会话的前提下切换视频 fixture |
| `capture.screenshot` | 采集一帧 PNG |
| `capture.video.start` / `capture.video.stop` | 采集屏幕视频 |
| `capture.audio.start` / `capture.audio.stop` | 采集扬声器或系统音频 |
| `wait` | 等待指定毫秒数 |

没有 `click`、`launch_app`、`expectation`、`assertion`、`build` 或任何业务 action。若调用方
需要在媒体序列前后操控 APP，应由调用方自己的执行器完成，再把 Harness 用作媒体 I/O 服务。

### 2.3 输出：原始媒体优先

| 输出 kind | 标准输出 | 说明 |
|---|---|---|
| `screen.image` | PNG | 截图原件 |
| `screen.video` | MP4/WebM + 编码元数据 | 屏幕视频原件；不默认代表有音频 |
| `speaker.audio` | PCM/WAV + 采样率/声道 | 扬声器或系统音频原件 |
| `execution.log` | JSON Lines | 动作、时间戳、adapter 回执（已脱敏） |
| `recognition.*` | JSON | 可选 OCR、图像特征、音量、频谱、ASR 等派生信号 |
| `stream.receipt` | JSON | 输出 profile、帧数、最大帧间隔、session restart 与断流计数 |

服务只报告“是否采集成功、资产是否完整、识别器产生了什么信号”。它不将识别结果转换成业务
结论，也不产生 `passed`、`failed` 或 `deviation`。

---

## 三、对调用方的接口

正式结构：

- [`schemas/media-run-request.schema.json`](../schemas/media-run-request.schema.json)
- [`schemas/media-run-result.schema.json`](../schemas/media-run-result.schema.json)

| 接口 | 输入 | 输出 | 语义 |
|---|---|---|---|
| `POST /v1/media-runs` | `MediaRunRequest` | `202 + run_id` | 提交一次媒体输入输出运行 |
| `GET /v1/media-runs/{run_id}` | run ID | `MediaRunResult` | 查询状态、输出媒体与识别索引 |
| `POST /v1/media-runs/{run_id}:cancel` | 取消原因 | 状态 | 停止尚未完成的采集或注入 |
| `GET /v1/media-capabilities?environment_ref=...` | 环境引用 | capability registry | 读取可实际使用的媒体能力 |
| `GET /v1/artifacts/{artifact_id}:access` | 身份 | 短期读取授权 | 获取已授权输出；不泄露云凭证 |

### 3.1 异步状态、幂等与设备租约

`POST /v1/media-runs` 只完成请求校验、输入快照持久化和 SQLite 状态登记，随后立即返回 `202`。
独立 Worker 从 SQLite 的 `media_runs` 队列领取任务；`idempotency_keys` 确保相同 key 只产生一个
run，`device_leases` 以 `environment_ref` 为粒度确保首期一个云机环境同一时刻最多运行一个媒体
任务。状态只能按下列路径迁移：

```text
queued -> running -> completed | capability_unavailable | error | canceled
```

Worker 重启时保留 `queued` 任务；已超过租约的 `running` 任务必须以 `worker_lease_expired` 进入
结构化 `error`，不得在未知的媒体会话上继续推帧。SQLite 是首期单服务进程的权威状态库；迁移到
多 Worker 时替换内部状态/租约实现，不改变公开 API。

`MediaRunRequest` 的最小示例：

```json
{
  "schema_version": "prismloop.media-run-request.v1",
  "request_id": "media-req-001",
  "idempotency_key": "external-task-9-attempt-1",
  "environment_ref": "cloud-phone/android-standard",
  "artifact_store_ref": "tos/prismloop-media",
  "inputs": [{
    "input_id": "camera-feed",
    "kind": "camera.video",
    "artifact": {
      "artifact_ref": "artifact://tos/prismloop-media/fixtures/camera/demo.mp4",
      "sha256": "<sha256>",
      "media_type": "video/mp4"
    }
  }],
  "camera_streams": [{
    "stream_id": "rear-camera",
    "initial_input_id": "camera-feed",
    "profile": {"width": 720, "height": 1280, "fps": 30, "pixel_format": "yuv420p"},
    "max_interframe_gap_ms": 67
  }],
  "sequence": [
    {"step_id": "stage", "action": "input.stage", "input_id": "camera-feed"},
    {"step_id": "open", "action": "camera.stream.open", "stream_id": "rear-camera"},
    {"step_id": "frame", "action": "capture.screenshot"},
    {"step_id": "close", "action": "camera.stream.close", "stream_id": "rear-camera"}
  ]
}
```

本地接口契约可用 `python -m src.media_server --demo --port 8787` 启动；这个启动模式只使用
内存资产库、SQLite 状态库与 recording adapter，专门验证 HTTP、异步幂等与无重开切换的 I/O 契约。它的环境引用为
`local/demo`，不能用于云机，也不会读取任何云凭证。实际 `VolcCloudPhoneAdapter` 完成外部原始帧
PoC 后，才允许部署配置选择对应的 provider bootstrap。

### 3.2 云机媒体探针 APK

仓库内的 `media-probe/` 是独立的诊断 APK，而不是 Harness 服务、业务 APP 或测试用例执行器。它只：

- 申请并打开 Android Camera2 后置摄像头预览，显示持续到达的预览帧时间戳；
- 使用 `AudioRecord` 打开麦克风，显示已读取 PCM 的格式、帧数和 RMS/dBFS；
- 在运行时清楚显示权限和设备错误，且不将音视频内容或业务结论上传。

因此，它提供“云机外部摄像头/麦克风输入是否确实到达 Android API”的最小观察点。Harness 将先用它完成
真实 Provider 的视频与 PCM 注入 PoC，再根据屏幕采集和可选扬声器采集的原始证据更新 capability registry。

---

## 四、测试数据与资产库

`ArtifactStore` 是存储抽象，首期适配火山 TOS。任何媒体输入和输出均使用同一种资产引用，
因此不同 Agent、不同设备和不同识别器都能复用同一素材与原始证据。

```text
artifact://<store-ref>/
├── fixtures/
│   ├── camera/<sha256>/<filename>
│   └── microphone/<sha256>/<filename>
└── media-runs/<run-id>/
    ├── input-snapshot.json
    ├── inputs/<input-id>/<normalized-media>
    ├── outputs/<step-id>/screen.png|screen.mp4|speaker.wav
    ├── recognition/<recognizer-id>.json
    ├── execution.jsonl
    └── evidence-index.json
```

`evidence-index.json` 必须记录每个资产的 ID、SHA-256、media type、产生时间、来源 adapter、
关联的 sequence step、保留期和脱敏等级。输出文件先持久化，随后才把 artifact ID 写入 result；
这样调用方拿到的永远是完整资产而不是一条可能失效的下载链接。

---

## 五、Adapter 与能力闸门

| 内部 Adapter | 职责 | 对调用方可见的抽象 |
|---|---|---|
| `ArtifactStore` | 取回 fixtures、保存输入快照与输出 | `artifact_ref` |
| `MediaInputAdapter` | 摄像头图像/视频、麦克风 PCM 注入 | `camera.*` / `microphone.*` |
| `VideoSourceRouter` | 预加载、补帧、转码、无断流切换视频 source | `camera_stream` / `stream.receipt` |
| `MediaCaptureAdapter` | 截图、屏幕视频、扬声器/系统音频采集 | `screen.*` / `speaker.audio` |
| `RecognizerAdapter` | OCR、图像特征、音量、频谱、ASR 等 | `recognition.*` JSON |
| `DeviceAdapter` | 资源借还、时钟、清理、厂商错误映射 | `environment_ref` |

运行开始前，服务依据 `inputs`、`sequence` 和 `recognizers` 计算所需能力。如果环境未被实测为
`verified`，服务返回 `capability_unavailable`，绝不伪装为成功或静默降级。

当前云手机 PoC 的能力账本：

| capability | 当前状态 | 证据 |
|---|---|---|
| `screen.image.capture` | `verified` | ACEP `BatchScreenShot` |
| `screen.video.capture` | `unverified` | 尚无通过 Harness capture adapter 的真实录屏证据 |
| `camera.video.inject` | `unverified` | `camera_file_preview` 仅静态诊断，不能代表外部帧注入 |
| `camera.continuous_stream_switch` | `unverified` | 需 SDK/原始帧流 PoC；ADB 文件路径切换不计入验证 |
| `microphone.pcm.inject` | `unverified` | 需客户端 SDK 或 Pod 原始流 PoC |
| `speaker.audio.capture` | `unverified` | 尚无独立云机采集与完整性证据 |

`camera.video.inject` 与 `microphone.pcm.inject` 是独立能力；不得把带 AAC 音轨的 MP4 注入
相机后，推断麦克风已经得到音频。

---

## 六、识别是可选派生输出

识别器消费已持久化的原始输出，产生版本化 JSON。例如：

```json
{
  "recognizer": "audio-signal@v1",
  "input_artifact_id": "art-...",
  "sample_rate_hz": 48000,
  "channels": 1,
  "mean_volume_db": -18.2,
  "dominant_frequencies_hz": [440.0],
  "transcript": null
}
```

这仅是信号观察，不是“语音指令通过”或“页面符合设计”。调用方可选择自己的识别器、阈值和
业务解释；同一原始媒体也可日后以新识别器重新处理，无需重跑云机。

---

## 七、环境变量与安全

| 配置类别 | 示例 | 规则 |
|---|---|---|
| 服务监听 | `PRISMLOOP_MEDIA_LISTEN_HOST` / `PRISMLOOP_MEDIA_LISTEN_PORT` | 非秘密；默认只监听本机 |
| 任务状态 | `PRISMLOOP_STATE_DB_PATH` | SQLite 文件路径；不存放媒体二进制或云凭证 |
| Artifact Store 身份 | `PRISMLOOP_ARTIFACT_STORE_CREDENTIAL_REF` | 仅服务进程从 Secret manager / Keychain 读取 |
| 云手机 profile | `PRISMLOOP_DEVICE_PROVIDER_PROFILE` | 仅服务部署配置引用 |
| 客户端 SDK Gateway | `PRISMLOOP_VEPHONE_CLIENT_GATEWAY_REF` | 私有 runtime 引用，不进入 MediaRun 请求 |
| 临时凭证角色 | `PRISMLOOP_VEPHONE_STS_ROLE_TRN` | 授权标识；短期 STS 凭证只在 gateway 内存中存在 |
| 默认资产库 | `PRISMLOOP_ARTIFACT_STORE_REF` | 允许覆写，但请求不含 bucket 凭证 |
| 调用参数 | `environment_ref`、`artifact_ref` | 非秘密、可审计 |

禁止将 AK/SK、ADB endpoint、Pod ID、永久 TOS URL、签名 URL 写入运行请求、媒体 fixture、
执行日志或结果。服务负责临时资源清理：停止输入、停止采集、释放设备租约、删除本地 staging；
存储中的不可变证据按 retention policy 管理。

`PodRawFrameAdapter` 和 `ClientExternalFrameAdapter` 是两条可替换的 Provider 内部边界。前者的
transport 必须由已验证的官方 SDK / 官方 bridge 实现；后者必须由已验证的官方客户端 SDK gateway
实现。两者都不允许假设自定义 REST、RTMP 或 ADB broadcast。未完成真实 PoC 前，能力账本保持
`unverified`。

---

## 八、服务验收标准

本服务的最小验收不是某个业务场景，而是下列 I/O 事实：

1. 给定一个校验过 SHA-256 的视频 fixture，能完成摄像头注入并输出一张可下载、校验过的截图；
2. 给定两个同 profile 的视频 fixture 和一个 `camera.stream.switch`，虚拟摄像头会话保持不变，输出 `stream.receipt` 的 restart/断流计数均为零，并可从原始捕获视频定位两段内容的切换；
3. 给定一个 PCM fixture，若能力为 `verified`，能按声明的采样率、声道、帧时长完成麦克风注入；否则明确返回 `capability_unavailable`；
4. 给定一个采集序列，能返回 screenshot / video / audio 的 artifact index，且每项可通过 SHA-256 验证；
5. 识别器异常、媒体缺失或设备故障必须有结构化错误和原始日志索引，不能将空输出当作成功；
6. 任何输出可由 run 的 `input-snapshot.json` 与 `execution.jsonl` 重放其媒体时序。
