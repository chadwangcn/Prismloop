"""Captured raw media is persisted before it appears in a MediaRun result."""

from __future__ import annotations

from types import SimpleNamespace

from src.media_capture import ACEPScreenCaptureAdapter
from src.media_capture import RecordingCaptureAdapter
from src.media_io import RecordingExternalVideoAdapter
from src.media_service import Capability, MediaRunService, MemoryArtifactStore

from .test_media_service import MappingVideoSourceResolver


def test_capture_actions_write_raw_outputs_and_evidence_index():
    store = MemoryArtifactStore()
    capture = RecordingCaptureAdapter()
    service = MediaRunService(
        artifact_store=store,
        video_adapter=RecordingExternalVideoAdapter(),
        video_source_resolver=MappingVideoSourceResolver({}),
        capture_adapter=capture,
        capabilities={
            "local/capture": {
                "screen.image.capture": Capability("screen.image.capture", "verified"),
                "screen.video.capture": Capability("screen.video.capture", "verified"),
                "speaker.audio.capture": Capability("speaker.audio.capture", "verified"),
            }
        },
    )
    request = {
        "schema_version": "prismloop.media-run-request.v1",
        "request_id": "media-request-capture",
        "idempotency_key": "capture-task-attempt-001",
        "environment_ref": "local/capture",
        "artifact_store_ref": "memory/prismloop-media",
        "inputs": [],
        "sequence": [
            {"step_id": "image", "action": "capture.screenshot"},
            {"step_id": "video-start", "action": "capture.video.start"},
            {"step_id": "video-stop", "action": "capture.video.stop"},
            {"step_id": "audio-start", "action": "capture.audio.start"},
            {"step_id": "audio-stop", "action": "capture.audio.stop"},
        ],
    }

    result = service.get(service.submit(request))

    assert result["status"] == "completed"
    assert [output["kind"] for output in result["outputs"]] == [
        "screen.image", "screen.video", "speaker.audio", "execution.log"
    ]
    assert capture.events == ["screenshot", "screen-video-start", "screen-video-stop", "speaker-audio-start", "speaker-audio-stop"]
    assert all(output["artifact"]["artifact_ref"] in store.objects for output in result["outputs"])


def test_acep_capture_preserves_actual_jpeg_media_type(monkeypatch):
    class FakeACEP:
        def batch_screen_shot(self, pod_ids, is_saved_on_pod):
            assert pod_ids == ["pod-1"]
            assert is_saved_on_pod is False
            return [{"url": "https://temporary.example/screenshot"}]

    class Resolver:
        def resolve_pod_id(self):
            return "pod-1"

    monkeypatch.setattr(
        "src.media_capture.requests.get",
        lambda url, timeout: SimpleNamespace(
            headers={"Content-Type": "image/jpeg; charset=binary"},
            content=b"\xff\xd8jpeg",
            raise_for_status=lambda: None,
        ),
    )

    captured = ACEPScreenCaptureAdapter(FakeACEP(), Resolver()).capture_screenshot()

    assert captured.media_type == "image/jpeg"
    assert captured.payload == b"\xff\xd8jpeg"
