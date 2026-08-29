"""The adapter transport uses a private loopback protocol only."""

from __future__ import annotations

import base64

import pytest

from src.audio_io import AudioProfile
from src.client_gateway_http import LoopbackClientGatewayTransport
from src.media_io import StreamProfile


class Response:
    def __init__(self, value): self._value = value
    def raise_for_status(self): return None
    def json(self): return self._value


class Session:
    def __init__(self): self.calls = []
    def post(self, url, *, json, timeout):
        self.calls.append((url, json, timeout))
        return Response({"sessionId": json.get("sessionId", "")})


def test_loopback_transport_forwards_standardized_video_and_pcm_frames():
    session = Session()
    gateway = LoopbackClientGatewayTransport(session=session)
    assert gateway.open_video("rear", StreamProfile(64, 64, 30)) == "rear"
    gateway.push_video("rear", b"frame", width=64, height=64, pixel_format="yuv420p", sequence_number=7, timestamp_us=100)
    assert gateway.open_audio("mic", AudioProfile(8000, 1, frame_duration_ms=20)) == "mic"
    gateway.push_audio("mic", b"\x00" * 320, sample_rate_hz=8000, channels=1, sample_format="s16le", sequence_number=8, timestamp_us=120)

    assert all(url.startswith("http://127.0.0.1:8791/v1/private/") for url, _, _ in session.calls)
    assert session.calls[1][1]["payloadBase64"] == base64.b64encode(b"frame").decode()
    assert "podId" not in session.calls[0][1]


def test_loopback_transport_refuses_non_loopback_destination():
    with pytest.raises(ValueError, match="loopback"):
        LoopbackClientGatewayTransport("https://provider.example")
