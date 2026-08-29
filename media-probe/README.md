# Prismloop Media Probe APK

这是云手机媒体输入的观察 APK，不是 Harness 服务、业务 APP 或测试用例执行器。

它在 Android 端打开 Camera2 后置摄像头预览并计数预览帧；同时以 48 kHz、单声道、s16le
格式读取 `AudioRecord`，显示样本计数、RMS 和 dBFS。它不保存媒体、不上传媒体，也不输出
“通过/失败”业务结论。

## 构建

需要 Android SDK Platform 34、Build Tools 34 和 JDK 17：

```sh
ANDROID_HOME=/opt/android-sdk gradle --no-daemon :app:assembleDebug
```

APK 输出为 `app/build/outputs/apk/debug/app-debug.apk`。当前仓库不保存构建产物；本地取回的
候选包放在已忽略的 `build-artifacts/`。

## 云机 PoC 使用边界

1. 通过服务外部的设备管理流程安装并启动此 APK，授予 CAMERA 与 RECORD_AUDIO。
2. Harness 在同一云机上以实际已验证的 Provider adapter 推入摄像头帧与 PCM 麦克风帧。
3. Harness 采集该 APK 的屏幕原件；屏幕上的 Camera2 帧计数与 `AudioRecord` 音量是输入已到达
   Android API 的观察信号。
4. 由调用方依据原始截图、屏幕录像和音频证据做业务判断；Probe 自身不承担判断。
