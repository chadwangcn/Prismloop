# Prismloop

自动开发与测试 harness:从设计稿到代码、构建、验证、截图比对、偏差反馈的**开发闭环系统**。

名字含义:棱镜(Prism)折射出实现与设计的偏差,循环(loop)驱动持续迭代。

## 核心定位

```
Figma 设计稿 ──► Coding ──► Build ──► 云手机部署
                                          │
偏差反馈 ◄── Visual Diff ◄── 截图采集 ◄──┘
   │
   └──► 开发 Agent 修复 ──► 下一轮循环
```

第一条已验证的核心能力:**云手机媒体注入与采集**——向虚拟摄像头/麦克风灌入指定序列的视频帧与 PCM 音频,同时抓取输出。

## 已验证能力(2026-08-31 PoC 实测)

| 能力 | 状态 | 实现路径 |
|---|---|---|
| 摄像头视频帧注入 | ✅ | Pod 内 injector APP + Proxy SDK `putVideoFrame`(I420 帧流) |
| 无断流序列热切换 | ✅ | A→B 切换实测:帧计数连续、会话不重建 |
| 麦克风 PCM 注入 | ✅ | `recordByFile(TYPE_CIRCLE)` 循环注入 |
| 屏幕截图采集 | ✅ | ACEP `BatchScreenShot` + ADB screencap |
| 视觉 diff | ✅ | 像素级对比,偏差分类 + 严重度分级 |
| 屏幕录屏采集 | ⏳ | ACEP StartRecording 待排查 |

完整能力账本见 [architecture/11-Harness媒体输入输出服务.md](architecture/11-Harness媒体输入输出服务.md)。

## 目录结构

```
src/                 Python harness 核心
  acep_client.py       火山引擎云手机 ACEP OpenAPI 封装
  media_run_service.py 媒体输入输出编排(12 种 action)
  screenshot_collector.py 设计合约驱动的截图采集
  visual_differ.py     像素级视觉 diff
  credentials.py       Keychain 凭证管理
scripts/             可执行脚本
  poc_pod_injection.py     注入 PoC 一键验证
  prepare_media_fixture.py ffmpeg 预解码(I420 帧流 + PCM)
media-injector/      Pod 内注入器 APP(Android,Proxy SDK)
media-probe/         Pod 内观察点 APP(Camera2/AudioRecord 消费注入数据)
gateway-web/         Web SDK 注入网关(备选路径,非主线)
architecture/        架构设计文档(01-11)
tests/               单元测试
```

## 媒体注入链路(已验证)

```
Python harness
  │ adb push fixture(I420 帧流 / PCM,由 ffmpeg 预解码)
  │ adb forward tcp:18080
  ▼
Pod 内 injector APP(127.0.0.1:18080 HTTP 控制)
  │ Proxy SDK(libvdevice 本地虚拟设备节点)
  ▼
虚拟摄像头 / 虚拟麦克风
  ▼
被测 APP(或 media-probe)读取 ──► ACEP 截图取证 ──► Visual Diff
```

- 纯后台链路,无浏览器依赖
- 视频帧 33ms 节奏(I420 裸帧流),音频 PCM 循环(官方 `recordByFile`)
- injector 的 HTTP 控制协议见 [media-injector/README.md](media-injector/README.md)

## 快速开始

```bash
# 1. 环境配置
cp config/env.template.json config/env.local.json   # 填入真实 ProductId/Pod ID
# AK/SK 存 macOS Keychain(见 architecture/02)

# 2. 安装依赖
pip install -r requirements.txt

# 3. 生成注入 fixture(视频→I420 帧流,音频→PCM)
python3 scripts/prepare_media_fixture.py

# 4. 构建 injector APP(需先下载 Proxy SDK aar,见 media-injector/app/libs/)
JAVA_HOME=<jdk17> ANDROID_HOME=<sdk> gradle -p media-injector :app:assembleDebug

# 5. 运行注入 PoC(自动安装到 Pod + 注入 + 截图取证)
python3 scripts/poc_pod_injection.py

# 6. 运行单元测试
python -m pytest tests/ -q
```

## 前提条件

- 火山引擎云手机实例(注入需**旗舰型**规格,如 g2.8c16g.plus)
- [Proxy SDK](https://docs.volcengine.com/docs/6394/1129851)(proxysdk aar,手动下载放 `media-injector/app/libs/`)
- JDK 17 + Android SDK 34(APK 构建)
- ffmpeg / ffprobe(fixture 预解码与音频分析)
- Python 3.9+

## 架构文档

设计先行是本项目的最高原则。所有架构决策、API 契约、能力账本都保存在 [architecture/](architecture/) :

| 文档 | 内容 |
|---|---|
| 00-总体架构设计 | 系统总览与 harness 闭环主线 |
| 02-云手机与外设模拟 | Pod 规格、ADB 注入、物理按键映射 |
| 09-视觉验证与偏差分类 | 设计合约、截图采集、VisualDiffer |
| 11-Harness媒体输入输出服务 | 媒体注入/采集编排、能力账本(v2.1) |

## License

Private / Internal
