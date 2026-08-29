"""Contract tests for the business-neutral Media I/O Harness service."""

from __future__ import annotations

import json
import threading
from http.client import HTTPConnection

import pytest

from src.media_http import create_server
from src.audio_io import RecordingExternalAudioAdapter
from src.media_io import IterableVideoSource, RecordingExternalVideoAdapter, StreamProfile
from src.media_service import Capability, MediaRunService, MemoryArtifactStore
from src.media_sources import ArtifactAudioSourceResolver


class MappingVideoSourceResolver:
    """Test-only resolver that acts like pre-verified decoded fixture assets."""

    def __init__(self, frames_by_input_id):
        self._frames_by_input_id = frames_by_input_id

    def resolve(self, input_spec, profile):
        return IterableVideoSource(profile, self._frames_by_input_id[input_spec["input_id"]])


def request_payload():
    sha_a = "a" * 64
    sha_b = "b" * 64
    return {
        "schema_version": "prismloop.media-run-request.v1",
        "request_id": "media-request-001",
        "idempotency_key": "task-001-attempt-001",
        "environment_ref": "local/recording-video",
        "artifact_store_ref": "memory/prismloop-media",
        "inputs": [
            {
                "input_id": "source-a",
                "kind": "camera.video",
                "artifact": {"artifact_ref": "artifact://fixtures/a.mp4", "sha256": sha_a, "media_type": "video/mp4"},
            },
            {
                "input_id": "source-b",
                "kind": "camera.video",
                "artifact": {"artifact_ref": "artifact://fixtures/b.mp4", "sha256": sha_b, "media_type": "video/mp4"},
            },
        ],
        "camera_streams": [
            {
                "stream_id": "rear-camera",
                "initial_input_id": "source-a",
                "profile": {"width": 720, "height": 1280, "fps": 30, "pixel_format": "yuv420p"},
                "max_interframe_gap_ms": 40,
            }
        ],
        "sequence": [
            {"step_id": "stage-a", "action": "input.stage", "input_id": "source-a"},
            {"step_id": "stage-b", "action": "input.stage", "input_id": "source-b"},
            {"step_id": "open", "action": "camera.stream.open", "stream_id": "rear-camera"},
            {"step_id": "first-segment", "action": "wait", "duration_ms": 100},
            {"step_id": "switch", "action": "camera.stream.switch", "stream_id": "rear-camera", "input_id": "source-b"},
            {"step_id": "second-segment", "action": "wait", "duration_ms": 100},
            {"step_id": "close", "action": "camera.stream.close", "stream_id": "rear-camera"},
        ],
    }


def create_service(capabilities=None):
    adapter = RecordingExternalVideoAdapter()
    store = MemoryArtifactStore()
    service = MediaRunService(
        artifact_store=store,
        video_adapter=adapter,
        video_source_resolver=MappingVideoSourceResolver({"source-a": [b"a0", b"a1"], "source-b": [b"b0", b"b1"]}),
        capabilities=capabilities
        or {
            "local/recording-video": {
                "camera.video.inject": Capability("camera.video.inject", "verified"),
                "camera.continuous_stream_switch": Capability("camera.continuous_stream_switch", "verified"),
            }
        },
    )
    return service, adapter, store


def test_service_executes_pre_staged_switch_in_one_camera_session():
    service, adapter, store = create_service()

    run_id = service.submit(request_payload())
    result = service.get(run_id)

    assert result["status"] == "completed"
    assert len(adapter.opens) == 1
    assert [frame[-1] for frame in adapter.frames] == [b"a0", b"a1", b"a0", b"b0", b"b1", b"b0"]
    receipt = result["stream_receipts"][0]
    assert receipt["source_session_restarts"] == 0
    assert receipt["discontinuity_count"] == 0
    assert receipt["source_history"] == ["source-a", "source-b"]
    assert receipt["artifact"]["artifact_ref"] in store.objects
    assert result["evidence_index"]["artifact_ref"] in store.objects


def test_service_retries_are_idempotent_and_do_not_open_second_session():
    service, adapter, _ = create_service()
    payload = request_payload()

    first = service.submit(payload)
    second = service.submit(payload)

    assert second == first
    assert len(adapter.opens) == 1


def test_unverified_environment_returns_explicit_capability_result_without_opening_camera():
    service, adapter, _ = create_service(
        {"local/recording-video": {"camera.video.inject": Capability("camera.video.inject", "unverified")}}
    )

    result = service.get(service.submit(request_payload()))

    assert result["status"] == "capability_unavailable"
    assert "camera.continuous_stream_switch: unavailable" in result["error"]
    assert "camera.video.inject: unverified" in result["error"]
    assert adapter.opens == []


def test_http_service_exposes_accepted_run_and_queryable_result():
    service, _, _ = create_service()
    server = create_server(service, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        connection = HTTPConnection("127.0.0.1", port, timeout=2)
        body = json.dumps(request_payload()).encode()
        connection.request("POST", "/v1/media-runs", body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        accepted = json.loads(response.read())
        assert response.status == 202

        connection.request("GET", f"/v1/media-runs/{accepted['run_id']}")
        response = connection.getresponse()
        result = json.loads(response.read())
        assert response.status == 200
        assert result["status"] == "completed"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_public_request_rejects_provider_and_business_fields():
    service, _, _ = create_service()
    payload = request_payload()
    payload["pod_id"] = "must-not-cross-public-boundary"

    with pytest.raises(ValueError, match="unsupported request fields"):
        service.submit(payload)


def test_pcm_microphone_input_uses_independent_audio_adapter():
    store = MemoryArtifactStore()
    pcm = b"\x01" * 320
    stored_artifact = store.put_bytes("fixtures/tone.pcm", pcm, "audio/L16")
    artifact = {key: stored_artifact[key] for key in ("artifact_ref", "sha256", "media_type")}
    audio_adapter = RecordingExternalAudioAdapter()
    service = MediaRunService(
        artifact_store=store,
        video_adapter=RecordingExternalVideoAdapter(),
        video_source_resolver=MappingVideoSourceResolver({}),
        audio_adapter=audio_adapter,
        audio_source_resolver=ArtifactAudioSourceResolver(store),
        capabilities={"local/audio": {"microphone.pcm.inject": Capability("microphone.pcm.inject", "verified")}},
    )
    request = {
        "schema_version": "prismloop.media-run-request.v1",
        "request_id": "media-request-audio",
        "idempotency_key": "audio-task-attempt-001",
        "environment_ref": "local/audio",
        "artifact_store_ref": "memory/prismloop-media",
        "inputs": [{"input_id": "mic", "kind": "microphone.pcm", "artifact": artifact,
                    "audio_format": {"sample_rate_hz": 8000, "channels": 1, "sample_format": "s16le"}}],
        "sequence": [
            {"step_id": "stage", "action": "input.stage", "input_id": "mic"},
            {"step_id": "start", "action": "input.start", "input_id": "mic"},
            {"step_id": "emit", "action": "wait", "duration_ms": 20},
            {"step_id": "stop", "action": "input.stop", "input_id": "mic"},
        ],
    }

    result = service.get(service.submit(request))

    assert result["status"] == "completed"
    assert len(audio_adapter.opens) == 1
    assert audio_adapter.frames[0][-1] == pcm
