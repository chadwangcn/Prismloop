# Proxy SDK(vendored)

本目录的 `proxysdk-0.2.2.3.2.aar` 已随仓库入库(vendored),保证 clone 后可直接构建
`media-injector`,无需额外下载步骤。

## 来源与版本

- 官方文档:https://docs.volcengine.com/docs/6394/1129851 「下载 Proxy SDK」章节
- 版本:0.2.2.3.2(2026-08-31 下载)
- 用途:云手机 Pod 裸数据注入(`libvdevice` 本地虚拟设备节点),实际包名 `com.ss.device.*`
- 前提:实例规格需旗舰型;SDK 以 Android aar 形态发布,运行于 Pod 内部

## 升级方式

官方发布新版本时:

1. 从上述文档链接下载新 aar
2. 删除旧 aar,放入本目录
3. 若包名/类签名变化,只需修改 `PodProxy.java`(全工程唯一直接 import proxysdk 的文件),
   用 javap 校验签名:

```
unzip proxysdk-*.aar classes.jar -d /tmp/proxysdk
unzip /tmp/proxysdk/classes.jar -d /tmp/proxysdk-classes
javap /tmp/proxysdk-classes/com/ss/device/proxy/camera/CameraProxyManager.class
```
