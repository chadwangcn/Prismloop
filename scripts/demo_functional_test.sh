#!/usr/bin/env bash
# 功能测试与演示一键脚本(场景 1-4)
#
# 场景 1: 服务自检(healthz + 能力闸门)
# 场景 2: 注入链路校准(四象限图案,验证显示方向)
# 场景 3: 无断流热切换(两个图案切换,验证 discontinuity_count=0)
# 场景 4: APP 业务闭环(系统相机消费注入 + ui.dump/tap + 视频音频同时注入)
#
# 用法:
#   bash scripts/demo_functional_test.sh
# 可用环境变量覆盖:
#   BASE(默认 http://127.0.0.1:8787)
#   ENV_REF(默认 volc/pod/7680786006895319844)
#   ADB_TARGET(默认 122.228.93.167:10002,仅场景 4 授权用)
#
# 前提: 服务已运行(python3 -m src.media_server --provider pod-injector)

set -uo pipefail

BASE="${BASE:-http://127.0.0.1:8787}"
ENV_REF="${ENV_REF:-volc/pod/7680786006895319844}"
ADB_TARGET="${ADB_TARGET:-122.228.93.167:10002}"
TS="$(date +%Y%m%d%H%M%S)"
FIXDIR="data/artifacts/fixtures"
PASS=0; FAIL=0

say()  { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m[PASS]\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }

# ---------------------------------------------------------------- 工具函数

submit_run() {  # $1=json 文件 → 输出 run_id
  curl -s -m 15 -X POST "$BASE/v1/media-runs" -H 'Content-Type: application/json' \
    -d @"$1" | python3 -c 'import json,sys;print(json.load(sys.stdin)["run_id"])'
}

wait_run() {  # $1=run_id → 输出终态
  local rid="$1" st i
  for i in $(seq 1 90); do
    st="$(curl -s -m 8 "$BASE/v1/media-runs/$rid" \
      | python3 -c 'import json,sys;print(json.load(sys.stdin).get("status",""))' 2>/dev/null || true)"
    case "$st" in
      completed|error|canceled|capability_unavailable) echo "$st"; return 0 ;;
    esac
    sleep 5
  done
  echo "timeout"
}

run_output() {  # $1=run_id $2=step_id → 本地 artifact 路径
  curl -s -m 8 "$BASE/v1/media-runs/$1" | python3 -c "
import json,sys
d=json.load(sys.stdin)
for o in d.get('outputs',[]):
    if o.get('step_id')=='$2':
        print('data/artifacts/'+o['artifact']['artifact_ref'].split('local/prismloop-media/',1)[1])
        break"
}

run_receipt() {  # $1=run_id → discontinuity_count(无回执输出 -1)
  curl -s -m 8 "$BASE/v1/media-runs/$1" | python3 -c "
import json,sys
d=json.load(sys.stdin)
rs=d.get('stream_receipts',[])
print(rs[0].get('discontinuity_count') if rs else -1)"
}

# 四象限布局验证: $1=截图 $2=A|B
# A: 左上红/右上绿/左下蓝/右下白   B: 左上蓝/右上红/左下白/右下绿
check_layout() {
  python3 - "$1" "$2" <<'PY'
import sys
import numpy as np
from PIL import Image

img = np.asarray(Image.open(sys.argv[1]).convert('RGB'), dtype=int)
H, W = img.shape[:2]
r, g, b = img[:,:,0], img[:,:,1], img[:,:,2]
masks = {
    'RED':   (r>150)&(g<100)&(b<100),
    'GREEN': (g>100)&(r<50)&(b<50),
    'BLUE':  (b>150)&(r<50)&(g<50),
    'WHITE': (r>200)&(g>200)&(b>200),
}
# probe 白色背景会污染白色象限质心: 用 R/G/B 内容包围盒限定白色检测区域
union = masks['RED'] | masks['GREEN'] | masks['BLUE']
cys, cxs = np.nonzero(union)
if len(cys) > 800:
    y0, y1, x0, x1 = cys.min(), cys.max(), cxs.min(), cxs.max()
    wm = np.zeros_like(masks['WHITE'])
    wm[y0:y1+1, x0:x1+1] = masks['WHITE'][y0:y1+1, x0:x1+1]
    masks['WHITE'] = wm
found = {}
# 质心以预览内容包围盒中心为基准(预览可能不居中)
cx, cy = ((x0+x1)/2, (y0+y1)/2) if len(cys) > 800 else (W/2, H/2)
for name, m in masks.items():
    ys, xs = np.nonzero(m)
    if len(ys) > 800:                      # 面积足够才算该色存在
        found[name] = (xs.mean() < cx, ys.mean() < cy)  # (左半?, 上半?)
expect = {
    'A': {'RED': (True, True),  'GREEN': (False, True),  'BLUE': (True, False),  'WHITE': (False, False)},
    'B': {'BLUE': (True, True), 'RED':   (False, True),  'WHITE': (True, False), 'GREEN': (False, False)},
}[sys.argv[2]]
for name, pos in expect.items():
    if found.get(name) != pos:
        print(f'layout mismatch: {name} expected {pos}, got {found.get(name)}')
        sys.exit(1)
print(f'layout {sys.argv[2]} 正立 ✓')
PY
}

sha256() { shasum -a 256 "$1" | cut -d' ' -f1; }

# ---------------------------------------------------------------- 场景 1

say "场景 1/4 服务自检"
if ! curl -s -m 5 "$BASE/healthz" >/dev/null 2>&1; then
  bad "服务不在线: $BASE —— 先启动: PRISMLOOP_POD_ID=... python3 -m src.media_server --provider pod-injector"
  exit 1
fi
ok "healthz 在线"
CAPS="$(curl -s -m 8 "$BASE/v1/media-capabilities?environment_ref=$ENV_REF")"
NEED="camera.video.inject camera.continuous_stream_switch microphone.pcm.inject screen.image.capture ui.interact ui.tree"
MISSING=""
for c in $NEED; do
  echo "$CAPS" | grep -q "\"name\": *\"$c\", *\"state\": *\"verified\"" || MISSING="$MISSING $c"
done
if [ -z "$MISSING" ]; then
  ok "能力闸门: 6 项核心能力全部 verified"
else
  bad "能力缺失:$MISSING"
fi
echo "$CAPS" | python3 -m json.tool | grep -E '"name"|"state"' | paste - - | sed 's/"name": //;s/"state": //;s/[",]//g' | sed 's/^/    /'

# ---------------------------------------------------------------- fixture 准备

say "准备演示 fixture(幂等)"
mkdir -p "$FIXDIR/camera" "$FIXDIR/audio" /tmp/prismloop-demo
# 图案 A(左上红/右上绿/左下蓝/右下白)
if [ ! -f "$FIXDIR/camera/quadrant1112.mp4" ]; then
  ffmpeg -y -v error -f lavfi -i "color=red:size=556x417:duration=1" -f lavfi -i "color=green:size=556x417:duration=1" \
    -f lavfi -i "color=blue:size=556x417:duration=1" -f lavfi -i "color=white:size=556x417:duration=1" \
    -filter_complex "[0][1]hstack[t];[2][3]hstack[b];[t][b]vstack,scale=1112:834,format=yuv420p" \
    -c:v libx264 -pix_fmt yuv420p "$FIXDIR/camera/quadrant1112.mp4"
fi
# 图案 B(左上蓝/右上红/左下白/右下绿) —— 与 A 布局不同,用于热切换对比
if [ ! -f "$FIXDIR/camera/quadrant1112b.mp4" ]; then
  ffmpeg -y -v error -f lavfi -i "color=blue:size=556x417:duration=1" -f lavfi -i "color=red:size=556x417:duration=1" \
    -f lavfi -i "color=white:size=556x417:duration=1" -f lavfi -i "color=green:size=556x417:duration=1" \
    -filter_complex "[0][1]hstack[t];[2][3]hstack[b];[t][b]vstack,scale=1112:834,format=yuv420p" \
    -c:v libx264 -pix_fmt yuv420p "$FIXDIR/camera/quadrant1112b.mp4"
fi
[ -f assets/audios/test-tone-440hz.m4a ] && cp -f assets/audios/test-tone-440hz.m4a "$FIXDIR/audio/"
SHA_A="$(sha256 $FIXDIR/camera/quadrant1112.mp4)"
SHA_B="$(sha256 $FIXDIR/camera/quadrant1112b.mp4)"
ok "fixture 就绪(图案 A/B + 音频)"

# ---------------------------------------------------------------- 场景 2

say "场景 2/4 注入链路校准(四象限方向验证)"
cat > /tmp/prismloop-demo/s2.json <<EOF
{
  "schema_version": "prismloop.media-run-request.v1",
  "request_id": "demo-s2-$TS",
  "idempotency_key": "demo-s2-$TS",
  "environment_ref": "$ENV_REF",
  "artifact_store_ref": "local/prismloop-media",
  "inputs": [{
    "input_id": "camera-feed",
    "kind": "camera.video",
    "artifact": {"artifact_ref": "artifact://local/prismloop-media/fixtures/camera/quadrant1112.mp4", "sha256": "$SHA_A", "media_type": "video/mp4"}
  }],
  "camera_streams": [{
    "stream_id": "rear-camera", "initial_input_id": "camera-feed",
    "profile": {"width": 1112, "height": 834, "fps": 24, "pixel_format": "yuv420p"},
    "max_interframe_gap_ms": 42
  }],
  "sequence": [
    {"step_id": "launch", "action": "ui.launch_app", "package": "cn.prismloop.mediaprobe", "activity": ".MainActivity"},
    {"step_id": "wait0", "action": "wait", "duration_ms": 2000},
    {"step_id": "stage", "action": "input.stage", "input_id": "camera-feed"},
    {"step_id": "open", "action": "camera.stream.open", "stream_id": "rear-camera"},
    {"step_id": "wait1", "action": "wait", "duration_ms": 3000},
    {"step_id": "shot", "action": "capture.screenshot"},
    {"step_id": "close", "action": "camera.stream.close", "stream_id": "rear-camera"}
  ],
  "timeout_seconds": 300
}
EOF
RID="$(submit_run /tmp/prismloop-demo/s2.json)"
ST="$(wait_run "$RID")"
if [ "$ST" = "completed" ]; then
  SHOT="$(run_output "$RID" shot)"
  if check_layout "$SHOT" A >/dev/null 2>&1; then
    ok "注入 → 截图 → 布局 A 正立 ($SHOT)"
  else
    bad "截图布局校验失败: $SHOT"
  fi
else
  bad "run 未完成: status=$ST (run_id=$RID)"
fi

# ---------------------------------------------------------------- 场景 3

say "场景 3/4 无断流热切换(A → B)"
cat > /tmp/prismloop-demo/s3.json <<EOF
{
  "schema_version": "prismloop.media-run-request.v1",
  "request_id": "demo-s3-$TS",
  "idempotency_key": "demo-s3-$TS",
  "environment_ref": "$ENV_REF",
  "artifact_store_ref": "local/prismloop-media",
  "inputs": [
    {"input_id": "pattern-a", "kind": "camera.video",
     "artifact": {"artifact_ref": "artifact://local/prismloop-media/fixtures/camera/quadrant1112.mp4", "sha256": "$SHA_A", "media_type": "video/mp4"}},
    {"input_id": "pattern-b", "kind": "camera.video",
     "artifact": {"artifact_ref": "artifact://local/prismloop-media/fixtures/camera/quadrant1112b.mp4", "sha256": "$SHA_B", "media_type": "video/mp4"}}
  ],
  "camera_streams": [{
    "stream_id": "rear-camera", "initial_input_id": "pattern-a",
    "profile": {"width": 1112, "height": 834, "fps": 24, "pixel_format": "yuv420p"},
    "max_interframe_gap_ms": 42
  }],
  "sequence": [
    {"step_id": "launch", "action": "ui.launch_app", "package": "cn.prismloop.mediaprobe", "activity": ".MainActivity"},
    {"step_id": "wait0", "action": "wait", "duration_ms": 2000},
    {"step_id": "stage-a", "action": "input.stage", "input_id": "pattern-a"},
    {"step_id": "stage-b", "action": "input.stage", "input_id": "pattern-b"},
    {"step_id": "open", "action": "camera.stream.open", "stream_id": "rear-camera"},
    {"step_id": "wait1", "action": "wait", "duration_ms": 3000},
    {"step_id": "shot-a", "action": "capture.screenshot"},
    {"step_id": "switch", "action": "camera.stream.switch", "stream_id": "rear-camera", "input_id": "pattern-b"},
    {"step_id": "wait2", "action": "wait", "duration_ms": 3000},
    {"step_id": "shot-b", "action": "capture.screenshot"},
    {"step_id": "close", "action": "camera.stream.close", "stream_id": "rear-camera"}
  ],
  "timeout_seconds": 300
}
EOF
RID="$(submit_run /tmp/prismloop-demo/s3.json)"
ST="$(wait_run "$RID")"
if [ "$ST" = "completed" ]; then
  DISC="$(run_receipt "$RID")"
  SHOT_A="$(run_output "$RID" shot-a)"
  SHOT_B="$(run_output "$RID" shot-b)"
  if [ "$DISC" = "0" ]; then
    ok "热切换无断流(discontinuity_count=0)"
  else
    bad "discontinuity_count=$DISC"
  fi
  if check_layout "$SHOT_A" A >/dev/null 2>&1; then
    ok "切换前: 布局 A 正立"
  else
    bad "切换前布局校验失败: $SHOT_A"
  fi
  if check_layout "$SHOT_B" B >/dev/null 2>&1; then
    ok "切换后: 布局 B 正立"
  else
    bad "切换后布局校验失败: $SHOT_B"
  fi
else
  bad "run 未完成: status=$ST (run_id=$RID)"
fi

# ---------------------------------------------------------------- 场景 4

say "场景 4/4 APP 业务闭环(系统相机 + UI 交互 + 音视频同时注入)"
# 相机/位置/麦克风权限预授(best-effort,已授权时报错可忽略)
# 位置权限不授会弹"允许相机获取位置信息"对话框挡住预览(2026-09-03 实测)
adb -s "$ADB_TARGET" shell pm grant com.android.camera2 android.permission.CAMERA >/dev/null 2>&1 || true
adb -s "$ADB_TARGET" shell pm grant com.android.camera2 android.permission.ACCESS_FINE_LOCATION >/dev/null 2>&1 || true
adb -s "$ADB_TARGET" shell pm grant com.android.camera2 android.permission.ACCESS_COARSE_LOCATION >/dev/null 2>&1 || true
adb -s "$ADB_TARGET" shell pm grant com.android.camera2 android.permission.RECORD_AUDIO >/dev/null 2>&1 || true
# 清掉上次运行遗留的待答复权限对话框(已授权后重启不会再次弹出)
adb -s "$ADB_TARGET" shell am force-stop com.android.camera2 >/dev/null 2>&1 || true
# 完成 APP 首次运行引导页("要记住照片拍摄地点吗?"→ 下一页),已完成的引导不会重现(幂等)
adb -s "$ADB_TARGET" shell "am start -n com.android.camera2/com.android.camera.CameraLauncher" >/dev/null 2>&1
sleep 2
adb -s "$ADB_TARGET" shell uiautomator dump /sdcard/window_dump.xml >/dev/null 2>&1
BTN="$(adb -s "$ADB_TARGET" shell cat /sdcard/window_dump.xml 2>/dev/null | python3 -c "
import re, sys
xml = sys.stdin.read()
m = re.search(r'text=\"(下一页|知道了|确定)\"[^>]*bounds=\"\[(\d+),(\d+)\]\[(\d+),(\d+)\]\"', xml)
if m:
    print((int(m.group(2))+int(m.group(4)))//2, (int(m.group(3))+int(m.group(5)))//2)
" 2>/dev/null || true)"
if [ -n "$BTN" ]; then
  adb -s "$ADB_TARGET" shell input tap $BTN
  sleep 1
fi
adb -s "$ADB_TARGET" shell am force-stop com.android.camera2 >/dev/null 2>&1 || true

SHA_TONE="$(sha256 $FIXDIR/audio/test-tone-440hz.m4a)"
cat > /tmp/prismloop-demo/s4.json <<EOF
{
  "schema_version": "prismloop.media-run-request.v1",
  "request_id": "demo-s4-$TS",
  "idempotency_key": "demo-s4-$TS",
  "environment_ref": "$ENV_REF",
  "artifact_store_ref": "local/prismloop-media",
  "inputs": [
    {"input_id": "camera-feed", "kind": "camera.video",
     "artifact": {"artifact_ref": "artifact://local/prismloop-media/fixtures/camera/quadrant1112.mp4", "sha256": "$SHA_A", "media_type": "video/mp4"}},
    {"input_id": "tone", "kind": "microphone.audio",
     "artifact": {"artifact_ref": "artifact://local/prismloop-media/fixtures/audio/test-tone-440hz.m4a", "sha256": "$SHA_TONE", "media_type": "audio/mp4"},
     "audio_format": {"sample_rate_hz": 48000, "channels": 2, "sample_format": "s16le"}}
  ],
  "camera_streams": [{
    "stream_id": "rear-camera", "initial_input_id": "camera-feed",
    "profile": {"width": 1112, "height": 834, "fps": 24, "pixel_format": "yuv420p"},
    "max_interframe_gap_ms": 42
  }],
  "sequence": [
    {"step_id": "launch", "action": "ui.launch_app", "package": "com.android.camera2"},
    {"step_id": "wait0", "action": "wait", "duration_ms": 3000},
    {"step_id": "stage", "action": "input.stage", "input_id": "camera-feed"},
    {"step_id": "stage-tone", "action": "input.stage", "input_id": "tone"},
    {"step_id": "open", "action": "camera.stream.open", "stream_id": "rear-camera"},
    {"step_id": "audio-on", "action": "input.start", "input_id": "tone"},
    {"step_id": "wait1", "action": "wait", "duration_ms": 3000},
    {"step_id": "dump", "action": "ui.dump"},
    {"step_id": "shot", "action": "capture.screenshot"},
    {"step_id": "tap", "action": "ui.tap", "x": 320, "y": 240},
    {"step_id": "wait2", "action": "wait", "duration_ms": 1500},
    {"step_id": "shot2", "action": "capture.screenshot"},
    {"step_id": "audio-off", "action": "input.stop", "input_id": "tone"},
    {"step_id": "close", "action": "camera.stream.close", "stream_id": "rear-camera"}
  ],
  "timeout_seconds": 300
}
EOF
RID="$(submit_run /tmp/prismloop-demo/s4.json)"
ST="$(wait_run "$RID")"
if [ "$ST" = "completed" ]; then
  DUMP="$(run_output "$RID" dump)"
  SHOT="$(run_output "$RID" shot)"
  if grep -q 'com.android.camera2' "$DUMP" 2>/dev/null; then
    ok "ui.dump: 控件树来自系统相机 ($DUMP)"
  else
    bad "dump 未包含相机控件树: $DUMP"
  fi
  # 相机预览区校验: 四色均大量出现(预览被相机 UI 部分遮挡,阈值放宽)
  python3 - "$SHOT" <<'PY' && ok "相机预览消费注入画面: 四色齐备 ($SHOT)" || bad "预览色彩校验失败: $SHOT"
import sys
import numpy as np
from PIL import Image
img = np.asarray(Image.open(sys.argv[1]).convert('RGB'), dtype=int)
r, g, b = img[:,:,0], img[:,:,1], img[:,:,2]
counts = {
    'red':   ((r>150)&(g<100)&(b<100)).sum(),
    'green': ((g>100)&(r<50)&(b<50)).sum(),
    'blue':  ((b>150)&(r<50)&(g<50)).sum(),
    'white': ((r>200)&(g>200)&(b>200)).sum(),
}
print({k: int(v) for k, v in counts.items()})
sys.exit(0 if all(v > 2000 for v in counts.values()) else 1)
PY
  SHOT2="$(run_output "$RID" shot2)"
  if [ -s "$SHOT2" ]; then
    ok "ui.tap + 交互后截图取证 ($SHOT2)"
  else
    bad "交互后截图缺失"
  fi
else
  bad "run 未完成: status=$ST (run_id=$RID)"
fi

# ---------------------------------------------------------------- 汇总

say "演示结果汇总"
printf 'PASS: %d  FAIL: %d\n' "$PASS" "$FAIL"
[ "$FAIL" = "0" ] && printf '\033[1;32m全部场景通过 ✓\033[0m\n' || printf '\033[1;31m存在失败项,详见上方日志\033[0m\n'
exit $([ "$FAIL" = "0" ] && echo 0 || echo 1)
