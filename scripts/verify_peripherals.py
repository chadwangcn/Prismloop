"""外设验证脚本:摄像头 / 麦克风 / 扬声器

依据架构文档 02-云手机与外设模拟.md:
- 第二章:虚拟摄像头注入(ADB 推 mp4 + am broadcast)
- 第三章:扬声器输出采集(ACEP start_recording + ffmpeg 分析)
- 第八章:音视频测试硬约束

流程:
1. 加载 ACEP + 启用 ADB
2. 检查广播接收器(com.volcengine.vphone.CAMERA_INJECT / AUDIO_INJECT)
3. 生成测试视频(640x480 mp4,含彩条 + 时间戳)
4. 生成测试音频(m4a,AAC-LC,440Hz 正弦波)
5. 推送视频/音频到云手机
6. 注入虚拟摄像头,启动 K1 APP 摄像头,截图验证
7. 注入虚拟麦克风,启动录音
8. 扬声器:ACEP 录屏 + ffmpeg 分析音频流
9. 输出验证报告 JSON

使用方式:
    python scripts/verify_peripherals.py [--pod-id <id>]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.config import load_config, create_acep_client
from src.acep_client import DEFAULT_POD_K1
from src.adb_client import ADBClient


# ============================================================================
# 常量
# ============================================================================


PACKAGE = "com.lumi.osdemo"
ASSETS_DIR = os.path.join(PROJECT_ROOT, "assets")
VIDEOS_DIR = os.path.join(ASSETS_DIR, "videos")
AUDIOS_DIR = os.path.join(ASSETS_DIR, "audios")
REPORTS_DIR = os.path.join(PROJECT_ROOT, "reports")

# 广播 action(架构文档 02-2.5)
ACTION_CAMERA_INJECT = "com.volcengine.vphone.CAMERA_INJECT"
ACTION_AUDIO_INJECT = "com.volcengine.vphone.AUDIO_INJECT"

# 测试视频参数(架构文档 02-2.3:640x480, mp4, MPEG-4)
TEST_VIDEO = os.path.join(VIDEOS_DIR, "test-pattern-640x480.mp4")
TEST_AUDIO = os.path.join(AUDIOS_DIR, "test-tone-440hz.m4a")


# ============================================================================
# 辅助函数
# ============================================================================


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(cmd: list[str], check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.stdout:
        print(f"    stdout: {result.stdout.strip()[:500]}")
    if result.stderr:
        # ffmpeg 写进度到 stderr,只显示末尾几行
        stderr_lines = result.stderr.strip().splitlines()
        if len(stderr_lines) > 5:
            print(f"    stderr: ...{stderr_lines[-3:]}")
        else:
            print(f"    stderr: {result.stderr.strip()[:500]}")
    if check and result.returncode != 0:
        raise RuntimeError(f"命令失败({result.returncode}): {' '.join(cmd)}")
    return result


def adb_shell(adb_target: str, cmd: str, timeout: int = 30) -> str:
    """执行 adb shell 命令,返回 stdout"""
    result = run(["adb", "-s", adb_target, "shell", cmd], check=False, timeout=timeout)
    return result.stdout.strip()


# ============================================================================
# 1. 生成测试媒体文件
# ============================================================================


def generate_test_video(path: str) -> bool:
    """生成 640x480 彩条测试视频(5 秒)"""
    if os.path.exists(path) and os.path.getsize(path) > 0:
        print(f"  测试视频已存在: {path}({os.path.getsize(path)} bytes)")
        return True
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # testsrc2 生成彩条 + 时间戳,640x480,5 秒,mp4
    run([
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", "testsrc2=size=640x480:rate=30:duration=5",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        path,
    ], check=True, timeout=30)
    print(f"  测试视频已生成: {path}({os.path.getsize(path)} bytes)")
    return True


def generate_test_audio(path: str) -> bool:
    """生成 440Hz 正弦波测试音频(3 秒,AAC-LC)"""
    if os.path.exists(path) and os.path.getsize(path) > 0:
        print(f"  测试音频已存在: {path}({os.path.getsize(path)} bytes)")
        return True
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # sine 生成 440Hz 正弦波,3 秒,AAC-LC
    run([
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", "sine=frequency=440:duration=3:sample_rate=44100",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ac", "1",  # 单声道
        path,
    ], check=True, timeout=30)
    print(f"  测试音频已生成: {path}({os.path.getsize(path)} bytes)")
    return True


# ============================================================================
# 2. 检查广播接收器
# ============================================================================


def check_broadcast_receivers(adb_target: str) -> dict:
    """查询云手机中注册的广播接收器"""
    print("\n[检查] 查询广播接收器...")
    # dumpsys package 查询接收器
    output = adb_shell(adb_target, "dumpsys package -r", timeout=30)

    receivers = {
        "camera_inject": ACTION_CAMERA_INJECT in output,
        "audio_inject": ACTION_AUDIO_INJECT in output,
    }
    # 查询 vphone 包是否存在
    vphone_pkgs = adb_shell(
        adb_target, "pm list packages | grep -i 'vphone\\|volcengine'",
        timeout=15,
    )
    receivers["vphone_packages"] = vphone_pkgs or "(none)"

    print(f"  CAMERA_INJECT 接收器: {'✓ 存在' if receivers['camera_inject'] else '✗ 不存在'}")
    print(f"  AUDIO_INJECT 接收器:  {'✓ 存在' if receivers['audio_inject'] else '✗ 不存在'}")
    print(f"  vphone 相关包: {receivers['vphone_packages']}")
    return receivers


# ============================================================================
# 3. 摄像头注入验证
# ============================================================================


def verify_camera_injection(
    adb_target: str,
    video_path: str,
    package: str,
    output_dir: str,
) -> dict:
    """验证摄像头注入

    使用 settings put global camera_file_preview 方式(官方文档推荐)。
    """
    print("\n[验证] 摄像头注入(settings put global 方式)...")
    result = {
        "name": "camera_injection",
        "method": "settings_put_global",
        "video_file": os.path.basename(video_path),
        "steps": [],
        "passed": False,
        "error": "",
    }

    # 1. 推送视频到云手机(路径不含中文)
    remote_dir = "/sdcard/playmp4"
    remote_video = f"{remote_dir}/{os.path.basename(video_path)}"
    adb_shell(adb_target, f"mkdir -p {remote_dir}", timeout=10)
    push_result = run(
        ["adb", "-s", adb_target, "push", video_path, remote_video],
        check=False, timeout=60,
    )
    result["steps"].append({
        "step": "push_video",
        "passed": push_result.returncode == 0,
        "output": push_result.stdout.strip()[:200],
    })
    if push_result.returncode != 0:
        result["error"] = "推送视频失败"
        return result

    # 2. 关闭注入(前置)
    adb_shell(adb_target, "settings put global camera_file_preview off", timeout=10)
    result["steps"].append({"step": "disable_injection", "passed": True})

    # 3. 设置视频文件路径
    set_path = adb_shell(
        adb_target,
        f"settings put global camera_file_preview_path {remote_video}",
        timeout=10,
    )
    result["steps"].append({
        "step": "set_video_path",
        "passed": True,
        "path": remote_video,
    })

    # 4. 设置循环播放
    adb_shell(adb_target, "settings put global camera_file_preview_loop true", timeout=10)
    result["steps"].append({"step": "set_loop", "passed": True})

    # 5. 开启注入
    adb_shell(adb_target, "settings put global camera_file_preview on", timeout=10)
    result["steps"].append({"step": "enable_injection", "passed": True})

    # 6. 启动 K1 APP
    launch_result = adb_shell(
        adb_target,
        f"monkey -p {package} -c android.intent.category.LAUNCHER 1",
        timeout=15,
    )
    result["steps"].append({
        "step": "launch_app",
        "passed": "Events injected" in launch_result,
        "output": launch_result[:200],
    })
    time.sleep(3)  # 等待 APP 启动

    # 7. 截图验证
    screenshot_path = os.path.join(output_dir, "camera-injected.png")
    adb_shell(adb_target, "screencap -p /sdcard/camera-injected.png", timeout=10)
    pull_result = run(
        ["adb", "-s", adb_target, "pull", "/sdcard/camera-injected.png", screenshot_path],
        check=False, timeout=30,
    )
    result["steps"].append({
        "step": "screenshot",
        "passed": pull_result.returncode == 0 and os.path.exists(screenshot_path),
        "local_path": screenshot_path,
    })

    # 8. 检查摄像头状态
    camera_status = adb_shell(adb_target, "dumpsys media.camera | grep -A5 'Camera '", timeout=15)
    result["steps"].append({
        "step": "check_camera_status",
        "passed": bool(camera_status),
        "output": camera_status[:800],
    })

    # 9. 检查 settings 是否生效
    settings_check = adb_shell(
        adb_target,
        "settings get global camera_file_preview",
        timeout=10,
    )
    result["steps"].append({
        "step": "verify_settings",
        "passed": settings_check.strip() == "on",
        "value": settings_check.strip(),
    })

    # 10. 停止注入
    adb_shell(adb_target, "settings put global camera_file_preview off", timeout=10)

    # 通过条件:截图成功 + settings 生效
    result["passed"] = (
        result["steps"][6]["passed"]  # screenshot
        and result["steps"][8]["passed"]  # settings 生效
    )
    print(f"  摄像头注入: {'✓ 通过' if result['passed'] else '✗ 失败'}")
    return result


# ============================================================================
# 4. 麦克风注入验证
# ============================================================================


def verify_mic_injection(
    adb_target: str,
    audio_path: str,
    output_dir: str,
) -> dict:
    """验证麦克风注入"""
    print("\n[验证] 麦克风注入...")
    result = {
        "name": "mic_injection",
        "audio_file": os.path.basename(audio_path),
        "steps": [],
        "passed": False,
        "error": "",
    }

    # 1. 推送音频
    remote_audio = "/sdcard/test-audio.m4a"
    push_result = run(
        ["adb", "-s", adb_target, "push", audio_path, remote_audio],
        check=False, timeout=60,
    )
    result["steps"].append({
        "step": "push_audio",
        "passed": push_result.returncode == 0,
        "output": push_result.stdout.strip()[:200],
    })
    if push_result.returncode != 0:
        result["error"] = "推送音频失败"
        return result

    # 2. 注入音频到虚拟麦克风
    inject_result = adb_shell(
        adb_target,
        f"am broadcast -a {ACTION_AUDIO_INJECT} "
        f'--es action "start" --es file "{remote_audio}"',
        timeout=15,
    )
    result["steps"].append({
        "step": "broadcast_audio_inject_start",
        "passed": "Broadcasting" in inject_result or "Result" in inject_result,
        "output": inject_result[:300],
    })

    # 3. 检查音频路由
    audio_routes = adb_shell(adb_target, "dumpsys audio | grep -A5 'routes'", timeout=15)
    result["steps"].append({
        "step": "check_audio_routes",
        "passed": bool(audio_routes),
        "output": audio_routes[:500],
    })

    # 4. 停止注入
    adb_shell(
        adb_target,
        f"am broadcast -a {ACTION_AUDIO_INJECT} --es action stop",
        timeout=10,
    )

    result["passed"] = result["steps"][1]["passed"]
    print(f"  麦克风注入: {'✓ 通过' if result['passed'] else '✗ 失败'}")
    return result


# ============================================================================
# 5. 扬声器验证(ACEP 录屏 + ffmpeg 分析)
# ============================================================================


def verify_speaker(
    acep,
    adb_target: str,
    pod_id: str,
    output_dir: str,
) -> dict:
    """验证扬声器:通过 ACEP 录屏,然后 ffmpeg 分析音频流"""
    print("\n[验证] 扬声器(ACEP 录屏 + ffmpeg 分析)...")
    result = {
        "name": "speaker_output",
        "steps": [],
        "passed": False,
        "error": "",
    }

    # 1. 启动 APP(让 K1 播放音效)
    # 注:K1 APP 可能不会主动播放音频,此处先录屏再看是否有音频流

    # 2. ACEP 开始录屏
    round_id = f"verify-speaker-{int(time.time())}"
    try:
        acep.start_recording(pod_id, duration_limit=15, round_id=round_id)
        result["steps"].append({
            "step": "start_recording",
            "passed": True,
            "round_id": round_id,
        })
    except Exception as e:
        result["error"] = f"start_recording 失败: {e}"
        result["steps"].append({"step": "start_recording", "passed": False, "error": str(e)})
        return result

    # 3. 等待录制 8 秒
    print("  录制中(8 秒)...")
    time.sleep(8)

    # 4. 停止录屏并下载
    try:
        resp = acep.stop_recording(pod_id)
        url = resp.get("url") or resp.get("path") if isinstance(resp, dict) else ""
        result["steps"].append({
            "step": "stop_recording",
            "passed": True,
            "url": url[:200] if url else "",
        })
    except Exception as e:
        result["error"] = f"stop_recording 失败: {e}"
        result["steps"].append({"step": "stop_recording", "passed": False, "error": str(e)})
        return result

    if not url or not url.startswith("http"):
        result["error"] = f"录屏 URL 无效: {url}"
        return result

    # 5. 下载录像
    recording_path = os.path.join(output_dir, "speaker-recording.mp4")
    run(["curl", "-sL", "-o", recording_path, url], check=True, timeout=60)
    result["steps"].append({
        "step": "download_recording",
        "passed": os.path.exists(recording_path) and os.path.getsize(recording_path) > 0,
        "local_path": recording_path,
        "size_bytes": os.path.getsize(recording_path) if os.path.exists(recording_path) else 0,
    })

    # 6. ffmpeg 分析音频流
    ffprobe_result = run(
        ["ffprobe", "-v", "error", "-show_streams", "-select_streams", "a",
         "-of", "json", recording_path],
        check=False, timeout=30,
    )
    has_audio = False
    audio_info = {}
    if ffprobe_result.returncode == 0:
        try:
            streams = json.loads(ffprobe_result.stdout).get("streams", [])
            if streams:
                has_audio = True
                audio_info = {
                    "codec": streams[0].get("codec_name"),
                    "sample_rate": streams[0].get("sample_rate"),
                    "channels": streams[0].get("channels"),
                    "duration": streams[0].get("duration"),
                }
        except json.JSONDecodeError:
            pass

    result["steps"].append({
        "step": "ffprobe_audio",
        "passed": has_audio,
        "audio_info": audio_info,
    })

    # 7. 音量检测
    volume_result = run(
        ["ffmpeg", "-i", recording_path, "-af", "volumedetect", "-f", "null", "/dev/null"],
        check=False, timeout=30,
    )
    mean_volume = None
    max_volume = None
    for line in volume_result.stderr.splitlines():
        if "mean_volume" in line:
            mean_volume = line.split(":")[-1].strip()
        if "max_volume" in line:
            max_volume = line.split(":")[-1].strip()
    result["steps"].append({
        "step": "volumedetect",
        "passed": mean_volume is not None,
        "mean_volume": mean_volume,
        "max_volume": max_volume,
    })

    result["passed"] = has_audio
    print(f"  扬声器: {'✓ 通过' if result['passed'] else '✗ 失败(无音频流)'}")
    if has_audio:
        print(f"    音频信息: {audio_info}")
        print(f"    平均音量: {mean_volume}, 峰值音量: {max_volume}")
    return result


# ============================================================================
# 主流程
# ============================================================================


def main():
    parser = argparse.ArgumentParser(description="外设验证:摄像头/麦克风/扬声器")
    parser.add_argument("--pod-id", default=None, help="K1 Pod ID(不传则从 config 读取 pods.k1.pod_id)")
    parser.add_argument("--package", default=PACKAGE, help="APP 包名")
    parser.add_argument("--skip-speaker", action="store_true", help="跳过扬声器验证")
    args = parser.parse_args()

    # 先加载 config 以获取真实 Pod ID
    config = load_config()
    pod_id = args.pod_id or config.pods.get("k1", {}).get("pod_id", DEFAULT_POD_K1)

    run_id = f"peripherals-{int(time.time())}"
    run_dir = os.path.join(REPORTS_DIR, run_id)
    os.makedirs(run_dir, exist_ok=True)

    print("=" * 60)
    print("Prismloop - 外设验证(摄像头/麦克风/扬声器)")
    print(f"Run ID:  {run_id}")
    print(f"Pod ID:  {pod_id}")
    print(f"Package: {args.package}")
    print(f"输出:    {run_dir}")
    print("=" * 60)

    # ----- 1. 加载 ACEP + ADB -----
    print("\n[1] 加载 ACEP + ADB...")
    acep = create_acep_client(config)
    acep.ensure_pods_powered_on([pod_id])
    adb_addr_obj = acep.get_adb_address(pod_id)
    adb_address = adb_addr_obj.address
    print(f"  ADB 地址: {adb_address}")

    adb_client = ADBClient()
    if ":" in adb_address:
        host, port_str = adb_address.rsplit(":", 1)
        adb_client.register_pod(pod_id, host, int(port_str))
    else:
        adb_client.register_pod(pod_id, adb_address, 5555)
    adb_target = adb_client.ensure_connected(pod_id)
    print(f"  ADB 已连接: {adb_target}")

    # ----- 2. 检查广播接收器 -----
    receivers = check_broadcast_receivers(adb_target)

    # ----- 3. 生成测试媒体 -----
    print("\n[2] 生成测试媒体文件...")
    generate_test_video(TEST_VIDEO)
    generate_test_audio(TEST_AUDIO)

    # ----- 4. 摄像头注入 -----
    print("\n[3] 摄像头注入验证...")
    camera_result = verify_camera_injection(
        adb_target, TEST_VIDEO, args.package, run_dir,
    )

    # ----- 5. 麦克风注入 -----
    print("\n[4] 麦克风注入验证...")
    mic_result = verify_mic_injection(
        adb_target, TEST_AUDIO, run_dir,
    )

    # ----- 6. 扬声器(ACEP 录屏 + ffmpeg 分析)-----
    speaker_result = None
    if not args.skip_speaker:
        print("\n[5] 扬声器验证...")
        speaker_result = verify_speaker(acep, adb_target, pod_id, run_dir)
    else:
        print("\n[5] 扬声器验证(跳过)")

    # ----- 7. 输出报告 -----
    print("\n[6] 输出验证报告...")
    report = {
        "run_id": run_id,
        "timestamp": now_iso(),
        "pod_id": pod_id,
        "package": args.package,
        "receivers": receivers,
        "results": {
            "camera": camera_result,
            "microphone": mic_result,
            "speaker": speaker_result,
        },
        "summary": {
            "camera_passed": camera_result["passed"] if camera_result else False,
            "mic_passed": mic_result["passed"] if mic_result else False,
            "speaker_passed": speaker_result["passed"] if speaker_result else False,
        },
    }
    report_path = os.path.join(run_dir, "peripherals-report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 60)
    print("外设验证完成")
    print(f"  摄像头: {'✓' if report['summary']['camera_passed'] else '✗'}")
    print(f"  麦克风: {'✓' if report['summary']['mic_passed'] else '✗'}")
    print(f"  扬声器: {'✓' if report['summary']['speaker_passed'] else '✗'}")
    print(f"  报告: {report_path}")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
