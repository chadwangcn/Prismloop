"""Durability, idempotency, and lease tests for asynchronous media runs."""

from __future__ import annotations

import time
import json
import threading
from http.client import HTTPConnection

from src.media_async import AsyncMediaRunService
from src.media_http import create_server
from src.media_io import IterableVideoSource, RecordingExternalVideoAdapter
from src.media_service import Capability, MemoryArtifactStore
from src.media_state import MediaRunStore

from .test_media_service import request_payload


class Resolver:
    def resolve(self, input_spec, profile):
        return IterableVideoSource(profile, [input_spec["input_id"].encode()])


def create_async_service(database_path=":memory:"):
    adapter = RecordingExternalVideoAdapter()
    state = MediaRunStore(database_path)
    service = AsyncMediaRunService(
        store=state,
        artifact_store=MemoryArtifactStore(),
        video_adapter=adapter,
        video_source_resolver=Resolver(),
        capabilities={
            "local/recording-video": {
                "camera.video.inject": Capability("camera.video.inject", "verified"),
                "camera.continuous_stream_switch": Capability("camera.continuous_stream_switch", "verified"),
            }
        },
        lease_seconds=1,
    )
    return service, state, adapter


def test_submit_only_queues_and_worker_executes_later():
    service, state, adapter = create_async_service()
    try:
        run_id = service.submit(request_payload())

        assert service.get(run_id)["status"] == "queued"
        assert adapter.opens == []
        assert service.run_once() is True
        assert service.get(run_id)["status"] == "completed"
        assert len(adapter.opens) == 1
        assert [event["event_type"] for event in state.events(run_id)] == ["accepted", "claimed", "finished"]
    finally:
        state.close()


def test_idempotent_submit_has_one_durable_run_and_one_execution():
    service, state, adapter = create_async_service()
    try:
        first = service.submit(request_payload())
        second = service.submit(request_payload())

        assert first == second
        service.run_once()
        assert len(adapter.opens) == 1
    finally:
        state.close()


def test_expired_running_lease_becomes_structured_error(tmp_path):
    service, state, _ = create_async_service(str(tmp_path / "media.sqlite"))
    try:
        run_id = service.submit(request_payload())
        assert state.claim_next("interrupted-worker", lease_seconds=0) == run_id
        time.sleep(0.01)

        assert list(state.recover_expired()) == [run_id]
        result = service.get(run_id)
        assert result["status"] == "error"
        assert result["error"] == "worker_lease_expired"
    finally:
        state.close()


def test_http_accepts_before_async_worker_completes_run():
    service, state, _ = create_async_service()
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
        assert service.get(accepted["run_id"])["status"] == "queued"
        assert service.run_once() is True
        connection.request("GET", f"/v1/media-runs/{accepted['run_id']}")
        response = connection.getresponse()
        assert json.loads(response.read())["status"] == "completed"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        state.close()
