# APK 构建流水线方案

> 文档版本:v1.0
> 创建日期:2026-08-06
> 适用范围:双 APK(Guardian.apk + K1-Device.apk)的构建、分发、安装

---

## 一、节点职责

### 1.1 节点角色

| 节点 | 角色 | 职责 |
|---|---|---|
| **构建云主机**(Build Host) | Android 编译节点 | Gradle 构建、APK 签名、产物归档、TOS 上传、webhook 通知 |
| **本地编排器** | 协调者 | 监听 webhook、触发测试、调度 Pod 安装 |
| **TOS 对象存储** | 产物中转 | 存储 APK + SHA256 + build-manifest.json |
| **云手机 Pod** | 测试执行节点 | 从 TOS 拉取 APK 并安装 |

### 1.2 设计原则

- **TOS 中转**:APK 不经本地中转,避免占用本地带宽和磁盘
- **webhook 通知**:构建完成后主动通知编排器,自动化程度最高
- **版本匹配**:双 APK 版本必须一致,契约 pin 绑定

---

## 二、双 APK 构建流程

### 2.1 总体流程

```
┌────────────────────────────────────────────────┐
│              构建云主机(Build Host)              │
├────────────────────────────────────────────────┤
│  git pull                                       │
│  ┌──────────────────────────────────────────┐  │
│  │  Module 1: Guardian APP                 │  │
│  │  ./gradlew :guardian:assembleDebug       │  │
│  │  → guardian-debug.apk                   │  │
│  │  → SHA256 + build-manifest.json         │  │
│  └──────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────┐  │
│  │  Module 2: K1 Device APP                │  │
│  │  ./gradlew :k1-device:assembleDebug     │  │
│  │  → k1-device-debug.apk                  │  │
│  │  → SHA256 + build-manifest.json         │  │
│  └──────────────────────────────────────────┘  │
│                                                 │
│  tos-cli cp guardian-debug.apk \                │
│    tos://<YOUR_BUILDS_BUCKET>/<sha>/guardian.apk    │
│  tos-cli cp k1-device-debug.apk \              │
│    tos://<YOUR_BUILDS_BUCKET>/<sha>/k1-device.apk   │
│                                                 │
│  curl -X POST http://local-orchestrator:8080/  │
│    webhook -d '{"commit":"<sha>","apks":      │
│    ["guardian.apk","k1-device.apk"]}'          │
└────────────────────────────────────────────────┘
```

### 2.2 构建脚本

```bash
#!/bin/bash
# scripts/build-dual-apk.sh
# 在构建云主机上执行

set -e

PROJECT_DIR="/path/to/Lumi-App-Android"
BUILD_OUTPUT="/tmp/build-output"
TOS_BUCKET="<YOUR_BUILDS_BUCKET>"
WEBHOOK_URL="http://local-orchestrator:8080/webhook"

# 1. 拉取最新代码
cd $PROJECT_DIR
git pull origin main
COMMIT_SHA=$(git rev-parse HEAD)

# 2. 清理旧产物
rm -rf $BUILD_OUTPUT
mkdir -p $BUILD_OUTPUT

# 3. 构建 Guardian APK
echo "Building Guardian APK..."
./gradlew :guardian:assembleDebug \
  -PbuildOutputDir=$BUILD_OUTPUT/guardian

GUARDIAN_APK=$BUILD_OUTPUT/guardian/build/outputs/apk/debug/guardian-debug.apk
if [ ! -f "$GUARDIAN_APK" ]; then
  echo "ERROR: Guardian APK build failed"
  exit 1
fi

# 4. 构建 K1 Device APK
echo "Building K1 Device APK..."
./gradlew :k1-device:assembleDebug \
  -PbuildOutputDir=$BUILD_OUTPUT/k1-device

K1_APK=$BUILD_OUTPUT/k1-device/build/outputs/apk/debug/k1-device-debug.apk
if [ ! -f "$K1_APK" ]; then
  echo "ERROR: K1 Device APK build failed"
  exit 1
fi

# 5. 计算 SHA256
GUARDIAN_SHA256=$(sha256sum $GUARDIAN_APK | awk '{print $1}')
K1_SHA256=$(sha256sum $K1_APK | awk '{print $1}')

# 6. 生成 build-manifest.json
cat > $BUILD_OUTPUT/build-manifest.json << EOF
{
  "commit_sha": "$COMMIT_SHA",
  "build_time": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "contract_pin": "gapp-m1-v1.2.0",
  "apks": {
    "guardian": {
      "path": "tos://$TOS_BUCKET/$COMMIT_SHA/guardian.apk",
      "sha256": "$GUARDIAN_SHA256",
      "version_name": "1.2.0",
      "min_k1_version": "1.2.0"
    },
    "k1_device": {
      "path": "tos://$TOS_BUCKET/$COMMIT_SHA/k1-device.apk",
      "sha256": "$K1_SHA256",
      "version_name": "1.2.0",
      "min_guardian_version": "1.2.0"
    }
  },
  "contract_compatibility": {
    "device_platform_api": "v2",
    "s2_guardian_api": "v1",
    "s5_content_api": "v1"
  }
}
EOF

# 7. 上传到 TOS
echo "Uploading to TOS..."
tos-cli cp $GUARDIAN_APK tos://$TOS_BUCKET/$COMMIT_SHA/guardian.apk
tos-cli cp $K1_APK tos://$TOS_BUCKET/$COMMIT_SHA/k1-device.apk
tos-cli cp $BUILD_OUTPUT/build-manifest.json tos://$TOS_BUCKET/$COMMIT_SHA/

# 8. 通知编排器
echo "Notifying orchestrator..."
curl -X POST $WEBHOOK_URL \
  -H "Content-Type: application/json" \
  -d "{
    \"commit_sha\": \"$COMMIT_SHA\",
    \"build_time\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",
    \"apks\": [\"guardian.apk\", \"k1-device.apk\"],
    \"manifest_path\": \"tos://$TOS_BUCKET/$COMMIT_SHA/build-manifest.json\"
  }"

echo "Build and upload complete. Commit: $COMMIT_SHA"
```

---

## 三、build-manifest.json 规范

### 3.1 结构

```json
{
  "commit_sha": "abc123def456...",
  "build_time": "2026-08-06T10:00:00Z",
  "contract_pin": "gapp-m1-v1.2.0",
  "apks": {
    "guardian": {
      "path": "tos://<YOUR_BUILDS_BUCKET>/abc123/guardian.apk",
      "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "version_name": "1.2.0",
      "version_code": 10200,
      "min_k1_version": "1.2.0"
    },
    "k1_device": {
      "path": "tos://<YOUR_BUILDS_BUCKET>/abc123/k1-device.apk",
      "sha256": "a1b2c3d4e5f6...",
      "version_name": "1.2.0",
      "version_code": 10200,
      "min_guardian_version": "1.2.0"
    }
  },
  "contract_compatibility": {
    "device_platform_api": "v2",
    "s2_guardian_api": "v1",
    "s5_content_api": "v1"
  }
}
```

### 3.2 字段说明

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `commit_sha` | string | 是 | Git commit SHA |
| `build_time` | string | 是 | 构建时间(UTC) |
| `contract_pin` | string | 是 | 契约版本 pin |
| `apks.guardian.path` | string | 是 | Guardian APK 在 TOS 的路径 |
| `apks.guardian.sha256` | string | 是 | Guardian APK 的 SHA256 |
| `apks.guardian.version_name` | string | 是 | Guardian 版本名 |
| `apks.guardian.version_code` | int | 是 | Guardian 版本号 |
| `apks.guardian.min_k1_version` | string | 是 | Guardian 最低兼容 K1 版本 |
| `apks.k1_device.path` | string | 是 | K1 APK 在 TOS 的路径 |
| `apks.k1_device.sha256` | string | 是 | K1 APK 的 SHA256 |
| `apks.k1_device.version_name` | string | 是 | K1 版本名 |
| `apks.k1_device.version_code` | int | 是 | K1 版本号 |
| `apks.k1_device.min_guardian_version` | string | 是 | K1 最低兼容 Guardian 版本 |
| `contract_compatibility` | object | 是 | 契约兼容性 |

---

## 四、webhook 通知协议

### 4.1 请求

```
POST http://local-orchestrator:8080/webhook
Content-Type: application/json
```

### 4.2 请求体

```json
{
  "event": "build_completed",
  "commit_sha": "abc123def456...",
  "build_time": "2026-08-06T10:00:00Z",
  "apks": ["guardian.apk", "k1-device.apk"],
  "manifest_path": "tos://<YOUR_BUILDS_BUCKET>/abc123/build-manifest.json"
}
```

### 4.3 编排器响应

```json
{
  "received": true,
  "message": "Build manifest received, starting test pipeline"
}
```

---

## 五、双 APK 安装流程

### 5.1 安装到 Pod

```python
def install_dual_apk(pair, build_manifest):
    """
    将双 APK 安装到配对的 Pod
    pair: Pod 配对信息
    build_manifest: 构建清单
    """
    # 1. 下载 build-manifest.json
    manifest = tos_client.get_object(
        build_manifest["manifest_path"]
    )
    
    # 2. 验证版本匹配
    assert_version_compatible(manifest)
    
    # 3. 安装 Guardian APK 到 guardian_pod
    guardian_apk_url = tos_client.get_signed_url(
        manifest["apks"]["guardian"]["path"]
    )
    mua.run_task(
        pod_id=pair["guardian_pod"],
        user_prompt=f"从 {guardian_apk_url} 下载 APK 并安装",
        max_step=20,
        timeout=120
    )
    
    # 4. 安装 K1 Device APK 到 k1_pod
    k1_apk_url = tos_client.get_signed_url(
        manifest["apks"]["k1_device"]["path"]
    )
    mua.run_task(
        pod_id=pair["k1_pod"],
        user_prompt=f"从 {k1_apk_url} 下载 APK 并安装",
        max_step=20,
        timeout=120
    )
    
    # 5. 验证双端版本匹配
    guardian_version = sdk.get_app_version(
        pair["guardian_pod"], "com.lumi.guardian"
    )
    k1_version = sdk.get_app_version(
        pair["k1_pod"], "com.lumi.k1device"
    )
    
    assert guardian_version == k1_version, \
        f"版本不匹配: Guardian={guardian_version}, K1={k1_version}"
    
    return {
        "guardian_version": guardian_version,
        "k1_version": k1_version,
        "manifest_sha": manifest["commit_sha"]
    }
```

### 5.2 版本匹配验证

```python
def assert_version_compatible(manifest):
    """验证双 APK 版本兼容性"""
    guardian = manifest["apks"]["guardian"]
    k1 = manifest["apks"]["k1_device"]
    
    # 版本名必须一致
    if guardian["version_name"] != k1["version_name"]:
        raise VersionMismatchError(
            f"Guardian {guardian['version_name']} != K1 {k1['version_name']}"
        )
    
    # 版本号必须一致
    if guardian["version_code"] != k1["version_code"]:
        raise VersionMismatchError(
            f"Guardian code {guardian['version_code']} != K1 code {k1['version_code']}"
        )
```

---

## 六、APK 更新流程

### 6.1 更新场景

Pod 上已安装旧版本 APK,需要更新到新版本。三种更新策略:

| 策略 | 命令 | 适用 | 数据保留 |
|---|---|---|---|
| **覆盖安装**(推荐) | `adb install -r` | 版本号递增 | 保留应用数据 |
| **降级安装** | `adb install -d` | 回退到旧版本 | 保留应用数据 |
| **卸载重装** | `adb uninstall` + `adb install` | 版本号降或签名变更 | 清除应用数据 |

### 6.2 覆盖安装流程(推荐)

```python
def update_dual_apk(pair, build_manifest):
    """更新双 APK(覆盖安装)"""
    manifest = tos_client.get_object(build_manifest["manifest_path"])
    
    # 1. 检查当前版本
    current_guardian = sdk.get_app_version(
        pair["guardian_pod"], "com.lumi.guardian"
    )
    current_k1 = sdk.get_app_version(
        pair["k1_pod"], "com.lumi.k1device"
    )
    
    target_guardian = manifest["apks"]["guardian"]["version_name"]
    target_k1 = manifest["apks"]["k1_device"]["version_name"]
    
    # 2. 判断是否需要更新
    if current_guardian == target_guardian and current_k1 == target_k1:
        return {"status": "already_up_to_date"}
    
    # 3. 覆盖安装 Guardian APK
    if current_guardian != target_guardian:
        guardian_apk_url = tos_client.get_signed_url(
            manifest["apks"]["guardian"]["path"]
        )
        # 下载 APK 到 Pod
        sdk.download_file(pair["guardian_pod"], guardian_apk_url, "/sdcard/guardian.apk")
        # 覆盖安装(-r 保留数据)
        sdk.adb_shell(pair["guardian_pod"], "pm install -r /sdcard/guardian.apk")
    
    # 4. 覆盖安装 K1 Device APK
    if current_k1 != target_k1:
        k1_apk_url = tos_client.get_signed_url(
            manifest["apks"]["k1_device"]["path"]
        )
        sdk.download_file(pair["k1_pod"], k1_apk_url, "/sdcard/k1-device.apk")
        sdk.adb_shell(pair["k1_pod"], "pm install -r /sdcard/k1-device.apk")
    
    # 5. 验证更新后版本
    new_guardian = sdk.get_app_version(pair["guardian_pod"], "com.lumi.guardian")
    new_k1 = sdk.get_app_version(pair["k1_pod"], "com.lumi.k1device")
    
    assert new_guardian == target_guardian, "Guardian 更新失败"
    assert new_k1 == target_k1, "K1 更新失败"
    
    return {
        "status": "updated",
        "guardian": {"from": current_guardian, "to": new_guardian},
        "k1": {"from": current_k1, "to": new_k1}
    }
```

### 6.3 更新失败处理

| 失败场景 | 处理 |
|---|---|
| 版本号相同(INSTALL_FAILED_VERSION_DOWNGRADE) | 改用 `-d` 降级安装 |
| 签名不一致(INSTALL_FAILED_UPDATE_INCOMPATIBLE) | 卸载后重装(会清除数据) |
| 存储空间不足(INSUFFICIENT_STORAGE) | 清理 Pod 缓存后重试 |
| APK 损坏 | 重新下载,校验 SHA256 |

```python
def safe_install(pod, apk_path, package_name):
    """安全安装(含失败处理)"""
    result = sdk.adb_shell(pod, f"pm install -r {apk_path}")
    
    if "INSTALL_FAILED_VERSION_DOWNGRADE" in result:
        # 降级安装
        result = sdk.adb_shell(pod, f"pm install -d -r {apk_path}")
    elif "INSTALL_FAILED_UPDATE_INCOMPATIBLE" in result:
        # 签名变更,卸载重装
        sdk.adb_shell(pod, f"pm uninstall {package_name}")
        result = sdk.adb_shell(pod, f"pm install {apk_path}")
    
    if "Success" not in result:
        raise InstallError(f"安装失败: {result}")
```

### 6.4 增量更新(可选优化)

对于频繁迭代场景,可采用增量更新减少下载量:

```bash
# 使用 bsdiff 生成差异包
bsdiff old.apk new.apk diff.patch

# Pod 上应用差异包
bspatch old.apk new.apk diff.patch
pm install -r new.apk
```

### 6.5 自动更新触发

测试系统监听构建 webhook,自动触发更新:

```python
def on_build_completed(webhook_payload):
    """构建完成回调,自动更新 Pod"""
    manifest_path = webhook_payload["manifest_path"]
    manifest = tos_client.get_object(manifest_path)
    
    # 获取所有活跃的 Pod 对
    active_pairs = pod_pool.get_active_pairs()
    
    for pair in active_pairs:
        # 异步更新每对 Pod
        threading.Thread(
            target=update_dual_apk,
            args=(pair, manifest)
        ).start()
```

---

## 七、TOS 对象存储结构

### 7.1 目录结构

```
tos://<YOUR_BUILDS_BUCKET>/
├── <commit-sha-1>/
│   ├── guardian.apk
│   ├── guardian.apk.sha256
│   ├── k1-device.apk
│   ├── k1-device.apk.sha256
│   └── build-manifest.json
├── <commit-sha-2>/
│   └── ...
└── latest -> <commit-sha-latest>  # 软链接指向最新
```

### 7.2 生命周期策略

- 保留最近 30 天的构建产物
- 超过 30 天的自动归档到低频存储
- 超过 90 天的自动删除

---

## 八、构建云主机配置要求

### 8.1 硬件要求

| 项 | 最低要求 | 推荐 |
|---|---|---|
| CPU | 8 核 | 16 核 |
| 内存 | 16 GB | 32 GB |
| 磁盘 | 100 GB SSD | 200 GB SSD |
| 网络 | 100 Mbps | 1 Gbps |

### 8.2 软件要求

| 项 | 版本 |
|---|---|
| OS | Ubuntu 22.04 LTS |
| JDK | OpenJDK 17 |
| Android SDK | API 34 |
| Gradle | 8.x |
| Git | 2.x |
| tos-cli | 最新版 |
| Android NDK | r25c(如需) |

### 8.3 签名管理

- **Debug 签名**:固定 keystore,存于构建云主机 `~/.android/debug.keystore`
- **Release 签名**:使用 KMS 或本地 keystore(不入 Git)
- **签名文件权限**:仅构建用户可读

---

## 九、异常处理

### 9.1 构建失败

| 失败场景 | 处理 |
|---|---|
| Gradle 编译错误 | 脚本退出,通知编排器构建失败 |
| APK 不存在 | 脚本退出,通知编排器 |
| TOS 上传失败 | 重试 3 次,指数退避 |
| webhook 通知失败 | 重试 3 次,记录日志供人工排查 |

### 9.2 安装失败

| 失败场景 | 处理 |
|---|---|
| TOS 下载失败 | 重试 3 次,检查签名 URL 有效期 |
| APK 安装失败 | 检查 Pod 存储空间,清理后重试 |
| 版本不匹配 | 终止测试,通知开发者 |
| SHA256 校验失败 | 终止测试,重新下载或重新构建 |

---

## 十、相关文档

- [00-总体架构设计.md](./00-总体架构设计.md)
- [01-MUA-OpenAPI集成方案.md](./01-MUA-OpenAPI集成方案.md)
- [06-证据链与报告规范.md](./06-证据链与报告规范.md)
