# 放置 Proxy SDK

从官方文档下载页获取 `proxysdk-0.2.2.3.2.aar`:

1. 打开 https://docs.volcengine.com/docs/6394/1129851
2. 「下载 Proxy SDK」章节 → 点击 **ProxySDK** 链接,下载 zip
3. 解压得到 `proxysdk-0.2.2.3.2.aar`
4. 把 aar 文件放到本目录(app/libs/)

放好后执行构建即可(依赖已配置 `fileTree(dir: "libs", include: ["*.jar", "*.aar"])`)。

若编译报 `package com.volcengine.proxysdk does not exist`:
解压 aar 查看 classes.jar 的实际包名,只需修改
`PodProxy.java` 顶部的 import(全工程唯一直接引用 proxysdk 的文件)。

查看包名方法:
```
unzip proxysdk-0.2.2.3.2.aar classes.jar -d /tmp/proxysdk
unzip /tmp/proxysdk/classes.jar -d /tmp/proxysdk-classes | head -50
```
