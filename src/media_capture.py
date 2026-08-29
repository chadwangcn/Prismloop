"""Business-neutral screen and system-audio capture boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Protocol

import requests

from .volc_pod import PodResolver


class MediaCaptureAdapter(Protocol):
    """Provider adapter selected internally from environment_ref configuration."""

    def capture_screenshot(self) -> "CapturedMedia":
        ...

    def start_screen_video(self) -> None:
        ...

    def stop_screen_video(self) -> "CapturedMedia":
        ...

    def start_speaker_audio(self) -> None:
        ...

    def stop_speaker_audio(self) -> "CapturedMedia":
        ...


@dataclass(frozen=True)
class CapturedMedia:
    payload: bytes
    media_type: str


class RecordingCaptureAdapter:
    """Deterministic fixture adapter for local service acceptance tests."""

    def __init__(self) -> None:
        self.events: List[str] = []

    def capture_screenshot(self) -> CapturedMedia:
        self.events.append("screenshot")
        return CapturedMedia(b"png-fixture", "image/png")

    def start_screen_video(self) -> None:
        self.events.append("screen-video-start")

    def stop_screen_video(self) -> CapturedMedia:
        self.events.append("screen-video-stop")
        return CapturedMedia(b"screen-video-fixture", "video/mp4")

    def start_speaker_audio(self) -> None:
        self.events.append("speaker-audio-start")

    def stop_speaker_audio(self) -> CapturedMedia:
        self.events.append("speaker-audio-stop")
        return CapturedMedia(b"speaker-audio-fixture", "audio/wav")


class ACEPScreenCaptureAdapter:
    """Real Volc ACEP screenshot adapter with no URL leakage to callers.

    Recording and speaker-audio methods deliberately remain unavailable until
    their cloud-storage/capture path has a verified Probe.  The service's
    capability gate keeps those methods outside a run before they are called.
    """

    def __init__(self, acep_client: Any, pod_resolver: PodResolver, timeout_seconds: int = 30) -> None:
        self._acep_client = acep_client
        self._pod_resolver = pod_resolver
        self._timeout_seconds = timeout_seconds

    def capture_screenshot(self) -> CapturedMedia:
        pod_id = self._pod_resolver.resolve_pod_id()
        results = self._acep_client.batch_screen_shot([pod_id], is_saved_on_pod=False)
        if len(results) != 1 or not results[0].get("url"):
            raise RuntimeError("ACEP screenshot did not return a temporary download URL")
        response = requests.get(results[0]["url"], timeout=self._timeout_seconds)
        response.raise_for_status()
        if not response.content:
            raise RuntimeError("ACEP screenshot download was empty")
        media_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if media_type not in {"image/png", "image/jpeg"}:
            raise RuntimeError(f"ACEP screenshot returned unsupported media type: {media_type or 'missing'}")
        return CapturedMedia(response.content, media_type)

    def start_screen_video(self) -> None:
        raise RuntimeError("screen video capture is not verified for this ACEP adapter")

    def stop_screen_video(self) -> CapturedMedia:
        raise RuntimeError("screen video capture is not verified for this ACEP adapter")

    def start_speaker_audio(self) -> None:
        raise RuntimeError("speaker audio capture is not verified for this ACEP adapter")

    def stop_speaker_audio(self) -> CapturedMedia:
        raise RuntimeError("speaker audio capture is not verified for this ACEP adapter")
