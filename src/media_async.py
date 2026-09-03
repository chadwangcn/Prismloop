"""Asynchronous SQLite-backed execution service for MediaRun HTTP requests."""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Mapping

from .media_io import ExternalVideoAdapter
from .media_service import (
    AudioSourceResolver,
    ArtifactStore,
    Capability,
    MediaRunConflictError,
    MediaRunService,
    MediaRunValidationError,
    VideoSourceResolver,
)
from .audio_io import ExternalAudioAdapter
from .media_capture import MediaCaptureAdapter
from .media_state import MediaRunStore


class AsyncMediaRunService:
    """Public service facade: accept quickly, then execute from a durable queue."""

    def __init__(
        self,
        *,
        store: MediaRunStore,
        artifact_store: ArtifactStore,
        video_adapter: ExternalVideoAdapter,
        video_source_resolver: VideoSourceResolver,
        capabilities: Mapping[str, Mapping[str, Capability]],
        audio_adapter: ExternalAudioAdapter | None = None,
        audio_source_resolver: AudioSourceResolver | None = None,
        capture_adapter: MediaCaptureAdapter | None = None,
        ui_adapter=None,
        wait_sleep: Callable[[float], None] | None = None,
        worker_id: str = "media-worker-1",
        lease_seconds: int = 60,
        poll_interval_seconds: float = 0.05,
    ) -> None:
        self._store = store
        self._artifact_store = artifact_store
        self._video_adapter = video_adapter
        self._video_source_resolver = video_source_resolver
        self._capabilities = {key: dict(value) for key, value in capabilities.items()}
        self._audio_adapter = audio_adapter
        self._audio_source_resolver = audio_source_resolver
        self._capture_adapter = capture_adapter
        self._ui_adapter = ui_adapter
        self._wait_sleep = wait_sleep
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._executor = self._new_executor()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._store.recover_expired()
        self._stop.clear()
        self._thread = threading.Thread(target=self._worker_loop, name=self._worker_id, daemon=True)
        self._thread.start()

    def stop(self, timeout_seconds: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_seconds)

    def submit(self, request: Mapping[str, Any]) -> str:
        MediaRunService._validate_request(request)
        run_id = f"media-run-{uuid.uuid4().hex}"
        initial_result = self._executor.prepare_result(run_id, request)
        accepted_run_id = self._store.create_run(run_id, request, initial_result)
        self._wake.set()
        return accepted_run_id

    def get(self, run_id: str) -> Dict[str, Any]:
        return self._store.get_result(run_id)

    def cancel(self, run_id: str, reason: str) -> Dict[str, Any]:
        try:
            return self._store.cancel(run_id, reason)
        except RuntimeError as exc:
            raise MediaRunConflictError(str(exc)) from exc

    def capabilities_for(self, environment_ref: str) -> List[Dict[str, str]]:
        return self._executor.capabilities_for(environment_ref)

    def run_once(self) -> bool:
        """Claim and execute one queued run; exposed for deterministic tests."""
        run_id = self._store.claim_next(self._worker_id, self._lease_seconds)
        if run_id is None:
            return False
        request = self._store.get_request(run_id)
        initial_result = self._store.get_result(run_id)
        try:
            result = self._new_executor().execute_assigned(run_id, request, initial_result)
            event_type = "finished" if result["status"] == "completed" else "finished_with_status"
        except Exception as exc:
            result = json.loads(json.dumps(initial_result))
            result["status"] = "error"
            result["error"] = f"worker_exception: {exc}"
            event_type = "worker_exception"
        self._store.complete(run_id, result, event_type)
        return True

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            if not self.run_once():
                self._wake.wait(self._poll_interval_seconds)
                self._wake.clear()

    def _new_executor(self) -> MediaRunService:
        return MediaRunService(
            artifact_store=self._artifact_store,
            video_adapter=self._video_adapter,
            video_source_resolver=self._video_source_resolver,
            capabilities=self._capabilities,
            audio_adapter=self._audio_adapter,
            audio_source_resolver=self._audio_source_resolver,
            capture_adapter=self._capture_adapter,
            ui_adapter=self._ui_adapter,
            wait_sleep=self._wait_sleep,
        )
