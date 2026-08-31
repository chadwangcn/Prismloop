# media-injector

Prismloop Media I/O Harness 的 Pod 内注入器:云机实例内常驻的 Android 前台服务,
通过火山引擎 Proxy SDK(裸数据注入)向本实例的虚拟摄像头/麦克风写入指定序列的
视频帧与 PCM 音频。

对应架构文档:`architecture/11-Harness媒体输入输出服务.md` §Pod 裸数据注入(v2.1 首选路径)。

## 组成

| 文件 | 职责 |
|---|---|
| `PodProxy.java` | Proxy SDK 唯一封装点(唯一 import proxysdk 的文件;官方 200ms 时序约束) |
| `FrameFeeder.java` | 帧序列播放器:manifest.json + frame_*.bin,33ms 节奏循环,支持无断流热切换 |
| `HttpApi.java` | 127.0.0.1:18080 控制通道(纯 ServerSocket,零第三方依赖) |
| `InjectorService.java` | 前台服务,组合上述三者 |
| `MainActivity.java` | 启动服务 + 状态显示 |

## 前提

- 云机实例规格:旗舰型(当前 prismloop-poc `g2.8c16g.plus` 满足)
- `app/libs/` 已放入 `proxysdk-0.2.2.3.2.aar`(见 libs/PUT-PROXYSDK-HERE.md)

## 构建

```
JAVA_HOME=<jdk17> ANDROID_HOME=<sdk> \
  /Users/hydramr/Documents/App-Dev/Lumi-App-Android/gradlew \
  --no-daemon -p media-injector :app:assembleDebug
```

## HTTP 控制协议(Python 侧经 adb forward)

```
adb forward tcp:18080 tcp:18080

GET  /status
POST /camera/sequence  {"dir":"/data/local/tmp/prismloop/fixtures/video-a","fps":30}
POST /camera/stop
POST /camera/frame     <binary frame>
POST /audio/file       {"path":"/data/local/tmp/prismloop/fixtures/tone.pcm","sampleRate":48000,"channels":2}
POST /audio/stop
```

## fixture 目录格式(Python 侧 ffmpeg 预解码生成)

```
/data/local/tmp/prismloop/fixtures/video-a/
  manifest.json      {"width":640,"height":480,"format":"i420","fps":30}
  frame_0000.bin     # I420 裸帧(w*h*3/2 字节)
  frame_0001.bin
  ...
```

## 注入链路

```
Python harness ──adb push fixture──► /data/local/tmp/prismloop/
Python harness ──adb forward:18080──► HttpApi ──► FrameFeeder ──► PodProxy
PodProxy ──putVideoFrame/putAudioFrame──► 虚拟设备节点(libvdevice)
media-probe(Camera2/AudioRecord)读取虚拟摄像头/麦克风 ──► BatchScreenShot 取证
```
