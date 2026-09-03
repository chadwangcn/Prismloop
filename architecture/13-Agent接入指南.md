# Agent 接入指南：Media I/O Harness 使用手册

> 文档版本：v1.0
> 更新日期：2026-09-02
> 适用读者：SDK 开发 Agent、APP 开发 Agent、测试 Agent 及任何需要媒体注入/采集能力的自动化调用方
> 前置阅读：[11-Harness媒体输入输出服务.md](./11-Harness媒体输入输出服务.md)（服务边界与契约定义）
>
> 本文件只讲"调用方怎么用"，不重复服务内部设计。API 字段定义以
> [schemas/media-run-request.schema.json](../schemas/media-run-request.schema.json)
> 与 [schemas/media-run-result.schema.json](../schemas/media-run-result.schema.json) 为权威。

---

## 一、这个服务是什么、不是什么

**是**：业务无关的媒体 I/O 服务。给它受版本保护的图片/视频/音频，它注入云手机摄像头/麦克风；给它采集序列，它返回截图/录像/音频等原始证据（SHA-256 校验、可重放）。

**不是**：不判断通过/失败，不理解 PRD/Figma/APK，不操作业务 APP 界面（无 click/launch）。调用方自己的执行器负责 APP 操控，Harness 只负责媒体进出。

调用方只需要认识两个概念：

| 概念 | 含义 | 示例 |
|---|---|---|
| `environment_ref` | 目标环境引用 | `cloud-phone/android-standard` |
| `artifact_ref` | 资产库内不可变媒体引用 | `artifact://tos/prismloop-media/fixtures/camera/demo.mp4` |

云手机 Pod、ADB 地址、AK/SK、存储 bucket 全部是服务内部细节，调用方不可见。

---

## 二、服务端点

| 接口 | 方法 | 语义 | 返回 |
|---|---|---|---|
| `/healthz` | GET | 探活（含部署自检） | 200 / 503 |
| `/v1/media-capabilities?environment_ref=...` | GET | 查询该环境实测可用的媒体能力 | capability 列表 |
| `/v1/media-runs` | POST | 提交一次媒体运行 | `202 + {"run_id": ...}` |
| `/v1/media-runs/{run_id}` | GET | 查询运行状态与输出索引 | `MediaRunResult` |
| `/v1/media-runs/{run_id}:cancel` | POST | 取消未完成的运行 | 状态 |

服务以内网地址常驻部署（监听 `0.0.0.0`，端口见部署配置）。调用方先打 `/healthz` 确认在线，再打 `/v1/media-capabilities` 确认所需能力为 `verified`——**`capability_unavailable` 是安全闸门，不要绕过**。

---

## 三、最小调用流程（四步）

```bash
BASE=http://<harness内网地址>

# 1. 探活
curl $BASE/healthz

# 2. 确认能力
curl "$BASE/v1/media-capabilities?environment_ref=cloud-phone/android-standard"

# 3. 提交运行（异步，立即返回 run_id）
curl -X POST $BASE/v1/media-runs -H 'Content-Type: application/json' -d @request.json
# → 202 {"run_id": "..."}

# 4. 轮询结果（建议 2~5s 间隔；终态后停止）
curl $BASE/v1/media-runs/<run_id>
```

### 请求示例：注入视频 + 采集截图

```json
{
  "schema_version": "prismloop.media-run-request.v1",
  "request_id": "sdk-debug-0042",
  "idempotency_key": "sdk-debug-0042-attempt-1",
  "environment_ref": "cloud-phone/android-standard",
  "artifact_store_ref": "tos/prismloop-media",
  "inputs": [{
    "input_id": "camera-feed",
    "kind": "camera.video",
    "artifact": {
      "artifact_ref": "artifact://tos/prismloop-media/fixtures/camera/quadrant.mp4",
      "sha256": "<64位sha256>",
      "media_type": "video/mp4"
    }
  }],
  "camera_streams": [{
    "stream_id": "rear-camera",
    "initial_input_id": "camera-feed",
    "profile": {"width": 1112, "height": 834, "fps": 24, "pixel_format": "yuv420p"},
    "max_interframe_gap_ms": 42
  }],
  "sequence": [
    {"step_id": "stage",  "action": "input.stage",        "input_id": "camera-feed"},
    {"step_id": "open",   "action": "camera.stream.open",  "stream_id": "rear-camera"},
    {"step_id": "wait1", "action": "wait", "duration_ms": 3000},
    {"step_id": "shot",  "action": "capture.screenshot"},
    {"step_id": "close", "action": "camera.stream.close",  "stream_id": "rear-camera"}
  ]
}
```

### UI 交互 action（APP 功能触发，冒烟 2026-09-03）

对被测 APP 做真实 UI 操作，与媒体注入编排组合成完整业务闭环。能力：`ui.interact`（tap/swipe/text/key/launch_app）、`ui.tree`（dump 控件树）。

| action | 必填字段 | 语义 |
|---|---|---|
| `ui.tap` | `x`, `y` | 单击坐标 |
| `ui.swipe` | `x1`,`y1`,`x2`,`y2`（可选 `duration_ms`） | 滑动 |
| `ui.text` | `value`（仅 ASCII） | 输入文本 |
| `ui.key` | `keycode`（4=BACK 66=ENTER…） | 按键 |
| `ui.launch_app` | `package`（可选 `activity`） | 启动 APP |
| `ui.dump` | — | 控件树 XML → 输出 `ui.tree` artifact |

典型用法（Agent 自主定位控件）：先 `ui.dump` 拿控件树 XML → 解析目标控件的 `bounds` 得到坐标 → `ui.tap` → `capture.screenshot` 验证结果：

```json
"sequence": [
  {"step_id": "launch", "action": "ui.launch_app", "package": "com.example.app"},
  {"step_id": "wait0",  "action": "wait", "duration_ms": 2000},
  {"step_id": "dump",   "action": "ui.dump"},
  {"step_id": "tap",    "action": "ui.tap", "x": 320, "y": 240},
  {"step_id": "shot",   "action": "capture.screenshot"}
]
```

> 说明：UI 触发/截图走 ADB 通道（`adb input` / `uiautomator` / `screencap`），非云手机 OpenAPI——后者截图接口（BatchScreenShot）实测异常且无触控指令接口，详见已知限制。

### 结果解读（关键字段）
status:                queued → running → completed | capability_unavailable | error | canceled
outputs[]:             每步采集产物（screen.image / screen.video / speaker.audio / execution.log）
                       每项含 artifact_id + artifact_ref + sha256，先落盘后登记，拿到即完整
stream_receipts[]:     连续流回执 —— source_session_restarts / discontinuity_count 必须为 0
                       才是无断流；帧数、最大帧间隔用于性能判断
input_snapshot:        本次运行的输入快照 artifact —— 重放依据
evidence_index:        全部证据的索引清单
error:                 结构化错误（终态非 completed 时必读）
```

---

## 四、闭环反馈与迭代协议

Harness 的每次 run 都自带重放依据，迭代闭环这样用：

1. **提交运行** → 拿 `run_id`，记录到自己的任务上下文；
2. **拿证据** → 终态后从 `outputs[]` / `evidence_index` 取截图/录像/receipt；
3. **自己判读** → 偏差分析、定位结论由调用方 Agent 产生（Harness 不做业务判断）；
4. **修复后重跑** → **相同请求体、只换 `idempotency_key`**（如 `-attempt-2`），得到新 run；
5. **对比验证** → 两次 run 的 `input_snapshot` 相同（输入一致），对比 `outputs[]` 差异即为修复效果。

约定：
- `idempotency_key` 相同的重复提交只产生一个 run（幂等）；
- 输入必须用 `artifact_ref` + `sha256` 引用，禁止本地路径/临时 URL，否则无法重放对比；
- 同一环境同一时刻只跑一个媒体任务（设备租约），任务排队是正常现象，不算错误。

---

## 五、问题定位诊断序列（SDK 问题 or APP 问题）

`media-probe` 是部署在云机内的**已知良好消费者**（只读 Camera2/AudioRecord，不做业务判断）。
定位"画面/声音不对"这类问题，按以下标准序列执行两个 run：

```text
Run A：标准 fixture（如四象限图案）注入 + media-probe 消费 + 截图
  │
  ├─ Run A 画面/声音异常 ──→ 注入链路问题（Proxy SDK / 虚拟设备 / 环境规格）
  │                          反馈对象：SDK 开发 Agent
  │                          证据：Run A 的 screenshot + stream_receipt + execution.log
  │
  └─ Run A 正常 ──→ Run B：同一 fixture、同一 sequence，换成被测 APP 消费 + 截图
        │
        ├─ Run B 异常 ──→ APP 侧问题（渲染矩阵/编解码/采样配置）
        │                 反馈对象：APP 开发 Agent
        │                 证据：Run A + Run B 的 input_snapshot 相同、outputs 差异
        │
        └─ Run B 也正常 ──→ 环境差异问题（如 sensorOrientation/竖屏 override）
```

要点：
- **两次 run 输入必须完全相同**（同 `artifact_ref`），差异只在消费者——`input_snapshot` 会替你证明这一点；
- 判读画面方向/内容时优先用**结构化证据**（四象限颜色分布、stream_receipt 帧计数），不要只靠肉眼；
- fixture 由 `scripts/prepare_media_fixture.py` 生成（含注入链路 180° 预旋转补偿，默认开启）。

### 标准诊断 fixture

| fixture | 用途 | 判读方式 |
|---|---|---|
| 四象限图案（红/绿/蓝/白） | 方向、镜像、比例 | 截图四象限颜色分布与期望比对 |
| 标准 4:3 横版视频（1112x834@24fps） | 比例、流畅度、内容 | 帧相关性匹配 / 人工复核 |
| 标准 PCM（48kHz 立体声） | 音频通路 | RMS/频谱识别结果 |

> 视频标准为 **4:3 横版**（与云机原生 640x480 横屏分辨率一致），已端到端验证：
> 三个 4:3 视频（10s/12s/6s）注入后均正立显示，且视频间无断流热切换正常。
> 注入链路仍恒叠加 180° 旋转，fixture 生成时默认预旋转补偿（`prepare_media_fixture.py`，
> `--no-rotate-180` 可禁用）。

> 注意：标准 fixture 当前尚未登记进资产库（位于仓库 `scripts/` 生成流程中），登记完成后以
> `artifact://.../fixtures/` 下的版本化引用为准。登记前使用本地生成方式，`sha256` 必须如实计算填写。

---

## 六、约束与安全红线

- 请求中**禁止**出现 AK/SK、ADB 地址、Pod ID、永久/签名 URL——出现即被拒（schema 校验 + 语义校验）；
- 云凭证只经服务进程环境注入，调用方接触不到；
- 采集/注入互斥：同一时刻同一环境只有一个注入源；切换前必须停用当前源（`camera.stream.switch` 除外，它是无断流热切换）；
- 超时上限 14400s；`timeout_seconds` 建议显式声明，超时 run 进入 `error` 并保留已采集证据。

---

## 七、已知限制（截至本文版本）

| 限制 | 影响 | 应对 |
|---|---|---|
| `GET /v1/artifacts/{artifact_id}:access` 未实现 | artifact 下载暂无统一授权接口 | 首期内网直连 TOS / 共享卷读取；接口落地后切换 |
| `screen.video.capture`、`speaker.audio.capture` 能力 `unverified` | 录屏/扬声器采集可能返回 `capability_unavailable` | 属预期行为，不代表故障 |
| 竖屏 APP UI 测试需 `wm size 480x640` override（重启失效） | 竖屏 APP 布局显示 | 由服务 bootstrap 重设；媒体注入测试用 4:3 横版视频 + 原生横屏，无此依赖 |
| 720x540 流尺寸注入画面损坏（白屏+碎片，2026-09-03 实测） | 特定尺寸下 SDK 缩放路径缺陷 | 注入 profile 统一使用 1112x834（已验证正立）|
| 截图采集的是云机当前前台 APP | 若前台是 injector/launcher，截图非预览画面 | 提交 run 前确保消费端 APP（如 media-probe）在前台 |

---

## 八、相关文档

- [11-Harness媒体输入输出服务.md](./11-Harness媒体输入输出服务.md)：服务边界、契约与能力账本
- [12-部署方案.md](./12-部署方案.md)：容器化部署与凭证注入
- [schemas/media-run-request.schema.json](../schemas/media-run-request.schema.json)：请求字段权威定义
- [schemas/media-run-result.schema.json](../schemas/media-run-result.schema.json)：结果字段权威定义
