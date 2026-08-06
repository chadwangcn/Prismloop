#!/usr/bin/env bash
# 双 APK 构建流水线脚本
#
# 依据架构文档 architecture/03-APK构建流水线.md 第二章
#
# 功能:
#   1. 拉取最新代码
#   2. 并行构建 Guardian APK 与 K1 Device APK
#   3. 计算 SHA256
#   4. 生成 build-manifest.json(含版本匹配校验)
#   5. 上传到 TOS
#   6. webhook 通知编排器
#
# 用法:
#   ./scripts/build-dual-apk.sh [选项]
#
# 选项:
#   --project-dir PATH    Android 项目根目录(默认: $LUMI_APP_ANDROID_DIR 或 /path/to/Lumi-App-Android)
#   --branch BRANCH       Git 分支(默认: main)
#   --build-type TYPE     构建类型 debug/release(默认: debug)
#   --tos-bucket NAME     TOS 存储桶名(默认: $TOS_BUILDS_BUCKET 或 <YOUR_BUILDS_BUCKET>)
#   --webhook-url URL     编排器 webhook URL(默认: $WEBHOOK_URL 或 http://localhost:8080/webhook)
#   --skip-upload         跳过 TOS 上传(仅本地构建)
#   --skip-notify         跳过 webhook 通知
#   -h, --help            显示帮助

set -euo pipefail

# ============================================================================
# 默认值
# ============================================================================

PROJECT_DIR="${LUMI_APP_ANDROID_DIR:-/path/to/Lumi-App-Android}"
BRANCH="main"
BUILD_TYPE="debug"
TOS_BUCKET="${TOS_BUILDS_BUCKET:-<YOUR_BUILDS_BUCKET>}"
WEBHOOK_URL="${WEBHOOK_URL:-http://localhost:8080/webhook}"
SKIP_UPLOAD=0
SKIP_NOTIFY=0
BUILD_OUTPUT="${BUILD_OUTPUT_DIR:-/tmp/build-output}"

# 日志函数
log() {
    local level="$1"
    shift
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [$level] $*"
}
info() { log "INFO" "$@"; }
warn() { log "WARN" "$@"; }
error() { log "ERROR" "$@"; }

# 解析参数
while [[ $# -gt 0 ]]; do
    case "$1" in
        --project-dir)   PROJECT_DIR="$2"; shift 2 ;;
        --branch)        BRANCH="$2"; shift 2 ;;
        --build-type)    BUILD_TYPE="$2"; shift 2 ;;
        --tos-bucket)    TOS_BUCKET="$2"; shift 2 ;;
        --webhook-url)   WEBHOOK_URL="$2"; shift 2 ;;
        --skip-upload)   SKIP_UPLOAD=1; shift ;;
        --skip-notify)   SKIP_NOTIFY=1; shift ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *)
            error "未知参数: $1"
            exit 1
            ;;
    esac
done

# ============================================================================
# 前置检查
# ============================================================================

check_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        error "未找到命令: $1"
        exit 1
    fi
}

check_command git
check_command gradle 2>/dev/null || true  # gradlew 也可

if [ ! -d "$PROJECT_DIR" ]; then
    error "Android 项目目录不存在: $PROJECT_DIR"
    error "请设置 LUMI_APP_ANDROID_DIR 环境变量或使用 --project-dir"
    exit 1
fi

if [ "$SKIP_UPLOAD" -eq 0 ]; then
    check_command tos-cli
fi

if [ "$SKIP_NOTIFY" -eq 0 ]; then
    check_command curl
fi

info "=========================================="
info "双 APK 构建流水线启动"
info "=========================================="
info "项目目录: $PROJECT_DIR"
info "分支: $BRANCH"
info "构建类型: $BUILD_TYPE"
info "TOS 桶: $TOS_BUCKET"
info "webhook: $WEBHOOK_URL"
info "构建输出: $BUILD_OUTPUT"
info "=========================================="

# ============================================================================
# 1. 拉取代码
# ============================================================================

info "[1/8] 拉取最新代码"
cd "$PROJECT_DIR"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull origin "$BRANCH"
COMMIT_SHA=$(git rev-parse HEAD)
COMMIT_SHORT="${COMMIT_SHA:0:12}"
info "当前 commit: $COMMIT_SHORT"

# ============================================================================
# 2. 清理旧产物
# ============================================================================

info "[2/8] 清理旧产物"
rm -rf "$BUILD_OUTPUT"
mkdir -p "$BUILD_OUTPUT/guardian" "$BUILD_OUTPUT/k1-device"

# ============================================================================
# 3. 构建 Guardian APK
# ============================================================================

info "[3/8] 构建 Guardian APK"

GRADLE_TASK_GUARDIAN=":guardian:assemble${BUILD_TYPE^}"
if ! ./gradlew "$GRADLE_TASK_GUARDIAN" \
    -PbuildOutputDir="$BUILD_OUTPUT/guardian" \
    --no-daemon -q; then
    error "Guardian APK 构建失败"
    exit 1
fi

# 查找构建产物(Guardian 可能是 guardian-debug.apk 或 app-debug.apk)
GUARDIAN_APK=$(find "$BUILD_OUTPUT/guardian" -name "*.apk" -type f | head -1)
if [ -z "$GUARDIAN_APK" ]; then
    # 回退到默认产物路径
    GUARDIAN_APK="app/build/outputs/apk/${BUILD_TYPE}/guardian-${BUILD_TYPE}.apk"
    if [ ! -f "$GUARDIAN_APK" ]; then
        GUARDIAN_APK=$(find . -name "guardian-${BUILD_TYPE}.apk" -type f 2>/dev/null | head -1)
    fi
fi
if [ -z "$GUARDIAN_APK" ] || [ ! -f "$GUARDIAN_APK" ]; then
    error "未找到 Guardian APK 产物"
    exit 1
fi
info "Guardian APK: $GUARDIAN_APK"

# 拷贝到统一输出目录
cp "$GUARDIAN_APK" "$BUILD_OUTPUT/guardian.apk"
GUARDIAN_APK="$BUILD_OUTPUT/guardian.apk"

# 提取版本信息(从 APK 的 manifest)
GUARDIAN_VERSION_NAME=$(aapt2 dump badging "$GUARDIAN_APK" 2>/dev/null | grep "versionName" | sed -e "s/.*versionName='//" -e "s/' .*//" || echo "0.0.0")
GUARDIAN_VERSION_CODE=$(aapt2 dump badging "$GUARDIAN_APK" 2>/dev/null | grep "versionCode" | sed -e "s/.*versionCode='//" -e "s/' .*//" || echo "0")

# ============================================================================
# 4. 构建 K1 Device APK
# ============================================================================

info "[4/8] 构建 K1 Device APK"

GRADLE_TASK_K1=":k1-device:assemble${BUILD_TYPE^}"
if ! ./gradlew "$GRADLE_TASK_K1" \
    -PbuildOutputDir="$BUILD_OUTPUT/k1-device" \
    --no-daemon -q; then
    error "K1 Device APK 构建失败"
    exit 1
fi

K1_APK=$(find "$BUILD_OUTPUT/k1-device" -name "*.apk" -type f | head -1)
if [ -z "$K1_APK" ]; then
    K1_APK="app/build/outputs/apk/${BUILD_TYPE}/k1-device-${BUILD_TYPE}.apk"
    if [ ! -f "$K1_APK" ]; then
        K1_APK=$(find . -name "k1-device-${BUILD_TYPE}.apk" -type f 2>/dev/null | head -1)
    fi
fi
if [ -z "$K1_APK" ] || [ ! -f "$K1_APK" ]; then
    error "未找到 K1 Device APK 产物"
    exit 1
fi
info "K1 Device APK: $K1_APK"

cp "$K1_APK" "$BUILD_OUTPUT/k1-device.apk"
K1_APK="$BUILD_OUTPUT/k1-device.apk"

K1_VERSION_NAME=$(aapt2 dump badging "$K1_APK" 2>/dev/null | grep "versionName" | sed -e "s/.*versionName='//" -e "s/' .*//" || echo "0.0.0")
K1_VERSION_CODE=$(aapt2 dump badging "$K1_APK" 2>/dev/null | grep "versionCode" | sed -e "s/.*versionCode='//" -e "s/' .*//" || echo "0")

# ============================================================================
# 5. 版本匹配校验
# ============================================================================

info "[5/8] 版本匹配校验"
if [ "$GUARDIAN_VERSION_NAME" != "$K1_VERSION_NAME" ]; then
    error "版本不匹配: Guardian=$GUARDIAN_VERSION_NAME, K1=$K1_VERSION_NAME"
    error "双 APK 版本必须一致(架构文档 03-第六章)"
    exit 1
fi
info "版本一致: $GUARDIAN_VERSION_NAME (code=$GUARDIAN_VERSION_CODE)"

# ============================================================================
# 6. 计算 SHA256
# ============================================================================

info "[6/8] 计算 SHA256"
GUARDIAN_SHA256=$(shasum -a 256 "$GUARDIAN_APK" | awk '{print $1}')
K1_SHA256=$(shasum -a 256 "$K1_APK" | awk '{print $1}')
info "Guardian SHA256: ${GUARDIAN_SHA256:0:16}..."
info "K1 SHA256:       ${K1_SHA256:0:16}..."

# ============================================================================
# 7. 生成 build-manifest.json
# ============================================================================

info "[7/8] 生成 build-manifest.json"

BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)
CONTRACT_PIN="gapp-${GUARDIAN_VERSION_NAME}"

cat > "$BUILD_OUTPUT/build-manifest.json" << EOF
{
  "commit_sha": "$COMMIT_SHA",
  "commit_short": "$COMMIT_SHORT",
  "build_time": "$BUILD_TIME",
  "build_type": "$BUILD_TYPE",
  "contract_pin": "$CONTRACT_PIN",
  "apks": {
    "guardian": {
      "path": "tos://$TOS_BUCKET/$COMMIT_SHA/guardian.apk",
      "sha256": "$GUARDIAN_SHA256",
      "version_name": "$GUARDIAN_VERSION_NAME",
      "version_code": $GUARDIAN_VERSION_CODE,
      "min_k1_version": "$K1_VERSION_NAME"
    },
    "k1_device": {
      "path": "tos://$TOS_BUCKET/$COMMIT_SHA/k1-device.apk",
      "sha256": "$K1_SHA256",
      "version_name": "$K1_VERSION_NAME",
      "version_code": $K1_VERSION_CODE,
      "min_guardian_version": "$GUARDIAN_VERSION_NAME"
    }
  },
  "contract_compatibility": {
    "device_platform_api": "v2",
    "s2_guardian_api": "v1",
    "s5_content_api": "v1"
  }
}
EOF
info "manifest 已生成: $BUILD_OUTPUT/build-manifest.json"

# ============================================================================
# 8. 上传到 TOS
# ============================================================================

if [ "$SKIP_UPLOAD" -eq 1 ]; then
    warn "[8/8] 跳过 TOS 上传(--skip-upload)"
else
    info "[8/8] 上传到 TOS"
    tos-cli cp "$GUARDIAN_APK" "tos://$TOS_BUCKET/$COMMIT_SHA/guardian.apk"
    tos-cli cp "$K1_APK" "tos://$TOS_BUCKET/$COMMIT_SHA/k1-device.apk"
    tos-cli cp "$BUILD_OUTPUT/build-manifest.json" "tos://$TOS_BUCKET/$COMMIT_SHA/"

    # 更新 latest 软链接
    tos-cli cp "$BUILD_OUTPUT/build-manifest.json" "tos://$TOS_BUCKET/latest/build-manifest.json" 2>/dev/null || \
        warn "更新 latest 软链接失败(可忽略)"
    info "TOS 上传完成"
fi

# ============================================================================
# 9. 通知编排器
# ============================================================================

if [ "$SKIP_NOTIFY" -eq 1 ]; then
    warn "[9/9] 跳过 webhook 通知(--skip-notify)"
else
    info "[9/9] 通知编排器: $WEBHOOK_URL"
    curl -sS -X POST "$WEBHOOK_URL" \
        -H "Content-Type: application/json" \
        -d "{
            \"event\": \"build_completed\",
            \"commit_sha\": \"$COMMIT_SHA\",
            \"commit_short\": \"$COMMIT_SHORT\",
            \"build_time\": \"$BUILD_TIME\",
            \"build_type\": \"$BUILD_TYPE\",
            \"version\": \"$GUARDIAN_VERSION_NAME\",
            \"apks\": [\"guardian.apk\", \"k1-device.apk\"],
            \"manifest_path\": \"tos://$TOS_BUCKET/$COMMIT_SHA/build-manifest.json\",
            \"guardian_sha256\": \"$GUARDIAN_SHA256\",
            \"k1_sha256\": \"$K1_SHA256\"
        }" || warn "webhook 通知失败(编排器可能未启动)"
    info "webhook 通知已发送"
fi

# ============================================================================
# 汇总
# ============================================================================

info "=========================================="
info "双 APK 构建完成"
info "=========================================="
info "commit:      $COMMIT_SHORT"
info "version:     $GUARDIAN_VERSION_NAME (code=$GUARDIAN_VERSION_CODE)"
info "build time:  $BUILD_TIME"
info "guardian:    $GUARDIAN_APK (${GUARDIAN_SHA256:0:16}...)"
info "k1-device:   $K1_APK (${K1_SHA256:0:16}...)"
info "manifest:    $BUILD_OUTPUT/build-manifest.json"
info "=========================================="
