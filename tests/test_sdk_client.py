"""SDK Client 单元测试"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from src.sdk_client import (
    SDKClient,
    SDKError,
    K1Key,
    HTTPTransport,
)


# ============================================================================
# K1Key 枚举
# ============================================================================


class TestK1Key:
    def test_keycodes_match_spec(self):
        """按键码必须匹配架构文档 02-第四章标定值"""
        assert K1Key.POWER.keycode == 26
        assert K1Key.VOLUME_UP.keycode == 24
        assert K1Key.VOLUME_DOWN.keycode == 25
        assert K1Key.AI.keycode == 67
        assert K1Key.CAMERA.keycode == 139

    def test_linux_names_match_spec(self):
        assert K1Key.POWER.linux_name == "KEY_POWER"
        assert K1Key.AI.linux_name == "KEY_F9"
        assert K1Key.CAMERA.linux_name == "KEY_F12"


# ============================================================================
# Transport
# ============================================================================


class TestHTTPTransport:
    def test_call_posts_json(self):
        transport = HTTPTransport("http://localhost:9090")
        with patch.object(transport.session, "post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"code": 0, "data": {"ok": True}}
            mock_resp.raise_for_status = MagicMock()
            mock_post.return_value = mock_resp

            result = transport.call("sendKeyCode", {"keycode": 67})

            assert result["data"]["ok"] is True
            mock_post.assert_called_once()
            url = mock_post.call_args[0][0]
            assert url == "http://localhost:9090/sendKeyCode"

    def test_call_strips_trailing_slash(self):
        transport = HTTPTransport("http://localhost:9090/")
        with patch.object(transport.session, "post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"code": 0}
            mock_resp.raise_for_status = MagicMock()
            mock_post.return_value = mock_resp

            transport.call("test", {})
            url = mock_post.call_args[0][0]
            assert url == "http://localhost:9090/test"

    def test_call_raises_on_network_error(self):
        transport = HTTPTransport("http://localhost:1")
        with patch.object(transport.session, "post") as mock_post:
            import requests
            mock_post.side_effect = requests.RequestException("connection refused")
            with pytest.raises(SDKError):
                transport.call("test", {})


# ============================================================================
# SDKClient(使用 mock transport)
# ============================================================================


@pytest.fixture
def sdk_client():
    transport = MagicMock()
    transport.call.return_value = {"code": 0, "data": {"ok": True}}
    return SDKClient(transport=transport)


class TestSDKClient:
    def test_send_key_code_builds_payload(self, sdk_client):
        sdk_client.send_key_code("pod-1", 67, "down_up", 100)
        args = sdk_client.transport.call.call_args
        method = args[0][0]
        payload = args[0][1]

        assert method == "sendKeyCode"
        assert payload["PodId"] == "pod-1"
        assert payload["keycode"] == 67
        assert payload["action"] == "down_up"
        assert payload["duration_ms"] == 100

    def test_send_k1_key_uses_correct_keycode(self, sdk_client):
        sdk_client.send_k1_key("pod-1", K1Key.AI)
        payload = sdk_client.transport.call.call_args[0][1]
        assert payload["keycode"] == 67  # AI key

    def test_send_mouse_key(self, sdk_client):
        sdk_client.send_mouse_key("pod-1", 540, 960, "down_up", "left")
        payload = sdk_client.transport.call.call_args[0][1]
        assert payload["x"] == 540
        assert payload["y"] == 960
        assert payload["button"] == "left"

    def test_swipe_generates_multi_touch(self, sdk_client):
        sdk_client.swipe("pod-1", 100, 200, 100, 600, duration_ms=300)
        payload = sdk_client.transport.call.call_args[0][1]
        assert payload["PodId"] == "pod-1"
        events = payload["events"]
        assert len(events) == 3
        assert events[0]["action"] == "down"
        assert events[1]["action"] == "move"
        assert events[2]["action"] == "up"
        assert events[1]["duration_ms"] == 300

    def test_send_ime_composition(self, sdk_client):
        sdk_client.send_ime_composition("pod-1", "测试文本")
        payload = sdk_client.transport.call.call_args[0][1]
        assert payload["text"] == "测试文本"

    def test_screenshot_saves_to_file(self, sdk_client, tmp_path):
        import base64

        image_bytes = b"\x89PNG\r\n\x1a\nfake_png"
        sdk_client.transport.call.return_value = {
            "code": 0,
            "data": base64.b64encode(image_bytes).decode(),
        }

        save_path = tmp_path / "shot.png"
        result = sdk_client.screenshot("pod-1", save_path=str(save_path))

        assert result == image_bytes
        assert save_path.exists()
        assert save_path.read_bytes() == image_bytes

    def test_screenshot_no_data_raises(self, sdk_client):
        sdk_client.transport.call.return_value = {"code": 0, "data": None}
        with pytest.raises(SDKError):
            sdk_client.screenshot("pod-1")

    def test_launch_app(self, sdk_client):
        sdk_client.launch_app("pod-1", "com.lumi.guardian")
        payload = sdk_client.transport.call.call_args[0][1]
        assert payload["package"] == "com.lumi.guardian"

    def test_launch_app_with_activity(self, sdk_client):
        sdk_client.launch_app("pod-1", "com.lumi.guardian", ".MainActivity")
        payload = sdk_client.transport.call.call_args[0][1]
        assert payload["activity"] == ".MainActivity"

    def test_inject_camera_video_requires_adb(self, sdk_client):
        sdk_client.adb_client = None
        with pytest.raises(Exception):
            sdk_client.inject_camera_video("pod-1", "/sdcard/test.mp4")

    def test_inject_camera_video_calls_adb_broadcast(self, sdk_client):
        adb = MagicMock()
        sdk_client.adb_client = adb
        adb.am_broadcast.return_value = "ok"

        sdk_client.inject_camera_video("pod-1", "/sdcard/test.mp4", "start")

        adb.am_broadcast.assert_called_once()
        call_args = adb.am_broadcast.call_args
        assert call_args[0][0] == "pod-1"
        assert call_args[0][1] == "com.volcengine.vphone.CAMERA_INJECT"
        extras = call_args[1]["extras"]
        assert extras["action"] == "start"
        assert extras["file"] == "/sdcard/test.mp4"

    def test_push_and_inject_video_full_flow(self, sdk_client, tmp_path):
        adb = MagicMock()
        sdk_client.adb_client = adb

        video_path = tmp_path / "test.mp4"
        video_path.write_bytes(b"fake_video")

        result = sdk_client.push_and_inject_video("pod-1", str(video_path))

        assert result == "/sdcard/test.mp4"
        adb.push.assert_called_once()
        adb.am_broadcast.assert_called_once()

    def test_push_and_inject_video_missing_file(self, sdk_client):
        adb = MagicMock()
        sdk_client.adb_client = adb

        with pytest.raises(Exception):
            sdk_client.push_and_inject_video("pod-1", "/nonexistent.mp4")


# ============================================================================
# 录像音频分析(使用 mock subprocess)
# ============================================================================


class TestAudioAnalysis:
    @pytest.fixture
    def sdk_with_ffmpeg(self):
        transport = MagicMock()
        return SDKClient(transport=transport)

    def test_parse_volumedetect_extracts_mean_and_max(self, sdk_with_ffmpeg):
        stderr = """[Parsed_volumedetect_0 @ 0x7f] mean_volume: -27.3 dB
[Parsed_volumedetect_0 @ 0x7f] max_volume: -3.1 dB
"""
        mean, max_db = sdk_with_ffmpeg._parse_volumedetect(stderr)
        assert mean == pytest.approx(-27.3)
        assert max_db == pytest.approx(-3.1)

    def test_parse_volumedetect_no_data(self, sdk_with_ffmpeg):
        mean, max_db = sdk_with_ffmpeg._parse_volumedetect("no volume data")
        assert mean == -100.0
        assert max_db == -100.0

    def test_analyze_recording_audio_passes_when_loud(self, sdk_with_ffmpeg, tmp_path):
        rec = tmp_path / "rec.mp4"
        rec.write_bytes(b"fake")

        with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("subprocess.run") as mock_run:
            # 第一次 ffprobe,第二次 ffmpeg volumedetect
            mock_run.side_effect = [
                MagicMock(stdout='{"streams":[{"sample_rate":"44100","channels":2,"duration":"3.5"}]}'),
                MagicMock(stderr="[Parsed_volumedetect_0] mean_volume: -15.0 dB\n[Parsed_volumedetect_0] max_volume: -2.0 dB\n"),
            ]

            result = sdk_with_ffmpeg.analyze_recording_audio(
                str(rec), min_volume_db=-30.0
            )

            assert result.passed is True
            assert result.mean_volume_db == pytest.approx(-15.0)
            assert result.max_volume_db == pytest.approx(-2.0)
            assert result.sample_rate == 44100
            assert result.channels == 2
            assert result.duration_sec == pytest.approx(3.5)

    def test_analyze_recording_audio_fails_when_silent(self, sdk_with_ffmpeg, tmp_path):
        rec = tmp_path / "rec.mp4"
        rec.write_bytes(b"fake")

        with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(stdout='{"streams":[]}'),
                MagicMock(stderr="[Parsed_volumedetect_0] mean_volume: -60.0 dB\n"),
            ]

            result = sdk_with_ffmpeg.analyze_recording_audio(
                str(rec), min_volume_db=-30.0
            )

            assert result.passed is False
            assert result.mean_volume_db == pytest.approx(-60.0)

    def test_analyze_raises_without_ffmpeg(self, sdk_with_ffmpeg, tmp_path):
        rec = tmp_path / "rec.mp4"
        rec.write_bytes(b"")
        with patch("shutil.which", return_value=None):
            with pytest.raises(SDKError):
                sdk_with_ffmpeg.analyze_recording_audio(str(rec))
