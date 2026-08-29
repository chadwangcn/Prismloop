"""Loopback-only transport to the managed Web SDK client gateway.

This is a provider-internal transport behind ``ClientExternalFrameAdapter``.
It deliberately has no Pod, SDK endpoint, token, or public MediaRun request
field.  The server is expected to bind only a loopback address on the same
host as the Harness worker.
"""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import urlparse

import requests

from .audio_io import AudioProfile
from .media_io import StreamProfile


class ClientGatewayTransportError(RuntimeError):
    """The private gateway rejected a lifecycle or frame operation."""


class LoopbackClientGatewayTransport:
    """HTTP implementation of ``ClientExternalFrameGateway`` for one worker."""

    def __init__(self, base_url: str = "http://127.0.0.1:8791", timeout_seconds: float = 10, session: Any = requests) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("client gateway must use an http loopback URL")
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._session = session

    def open_video(self, stream_id: str, profile: StreamProfile) -> str:
        return self._post("/v1/private/video-sessions", {
            "sessionId": stream_id,
            "width": profile.width,
            "height": profile.height,
            "fps": profile.fps,
            "pixelFormat": profile.pixel_format,
        })["sessionId"]

    def push_video(self, session_id: str, payload: bytes, **metadata: int | str) -> None:
        self._post("/v1/private/video-frames", {
            "sessionId": session_id,
            "width": int(metadata["width"]),
            "height": int(metadata["height"]),
            "pixelFormat": str(metadata["pixel_format"]),
            "sequenceNumber": int(metadata["sequence_number"]),
            "timestampUs": int(metadata["timestamp_us"]),
            "payloadBase64": base64.b64encode(payload).decode("ascii"),
        })

    def close_video(self, session_id: str) -> None:
        self._post("/v1/private/video-sessions:close", {"sessionId": session_id})

    def open_audio(self, source_id: str, profile: AudioProfile) -> str:
        return self._post("/v1/private/audio-sessions", {
            "sessionId": source_id,
            "sampleRateHz": profile.sample_rate_hz,
            "channels": profile.channels,
            "sampleFormat": profile.sample_format,
        })["sessionId"]

    def push_audio(self, session_id: str, payload: bytes, **metadata: int | str) -> None:
        self._post("/v1/private/audio-frames", {
            "sessionId": session_id,
            "sampleRateHz": int(metadata["sample_rate_hz"]),
            "channels": int(metadata["channels"]),
            "sampleFormat": str(metadata["sample_format"]),
            "sequenceNumber": int(metadata["sequence_number"]),
            "timestampUs": int(metadata["timestamp_us"]),
            "payloadBase64": base64.b64encode(payload).decode("ascii"),
        })

    def close_audio(self, session_id: str) -> None:
        self._post("/v1/private/audio-sessions:close", {"sessionId": session_id})

    def _post(self, path: str, body: dict[str, object]) -> dict[str, object]:
        try:
            response = self._session.post(f"{self._base_url}{path}", json=body, timeout=self._timeout_seconds)
            response.raise_for_status()
            value = response.json()
        except Exception as exc:
            raise ClientGatewayTransportError(f"client_gateway_operation_failed: {path}") from exc
        if not isinstance(value, dict):
            raise ClientGatewayTransportError(f"client_gateway_invalid_response: {path}")
        return value
