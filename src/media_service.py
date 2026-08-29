"""Business-neutral Media I/O Harness service core.

The service accepts only media-run contracts.  It has no concept of an APP,
test case, a user gesture, or a pass/fail business assertion.  Provider and
storage integrations are deliberately injected through small protocols, so
the first Volc cloud-phone adapter and a later Cuttlefish adapter expose the
same public run API.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol

from .audio_io import AudioFrameSource, AudioProfile, AudioSourceRouter, ExternalAudioAdapter
from .media_capture import MediaCaptureAdapter
from .media_io import (
    ExternalVideoAdapter,
    StreamProfile,
    VideoFrameSource,
    VideoSourceRouter,
)


class MediaRunValidationError(ValueError):
    """Raised when a request cannot be interpreted as a media-run contract."""


class MediaRunConflictError(RuntimeError):
    """Raised when a terminal run is asked to change state."""


@dataclass(frozen=True)
class Capability:
    """A non-secret, environment-specific capability declaration."""

    name: str
    state: str
    reason: str | None = None

    def to_dict(self) -> Dict[str, str]:
        result = {"name": self.name, "state": self.state}
        if self.reason:
            result["reason"] = self.reason
        return result


class ArtifactStore(Protocol):
    """Persistence boundary for immutable input snapshots and run evidence."""

    def put_json(self, path: str, value: Mapping[str, Any] | List[Any]) -> Dict[str, str]:
        ...

    def put_bytes(self, path: str, payload: bytes, media_type: str) -> Dict[str, str]:
        ...

    def get_bytes(self, artifact: Mapping[str, str]) -> bytes:
        ...


class VideoSourceResolver(Protocol):
    """Resolves an already verified input artifact into normalized raw frames."""

    def resolve(self, input_spec: Mapping[str, Any], profile: StreamProfile) -> VideoFrameSource:
        ...


class AudioSourceResolver(Protocol):
    """Resolves a verified microphone artifact into fixed-duration PCM frames."""

    def resolve(self, input_spec: Mapping[str, Any]) -> AudioFrameSource:
        ...


class MemoryArtifactStore:
    """Deterministic local store used by service-contract tests.

    It is intentionally not a TOS implementation.  The production TOS adapter
    must implement the same ``ArtifactStore`` protocol and persist before the
    result references an artifact.
    """

    def __init__(self, store_ref: str = "memory/prismloop-media") -> None:
        self._store_ref = store_ref
        self.objects: Dict[str, bytes] = {}

    def put_json(self, path: str, value: Mapping[str, Any] | List[Any]) -> Dict[str, str]:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return self.put_bytes(path, payload, "application/json")

    def put_bytes(self, path: str, payload: bytes, media_type: str) -> Dict[str, str]:
        digest = hashlib.sha256(payload).hexdigest()
        artifact_id = f"art-{digest[:16]}"
        artifact_ref = f"artifact://{self._store_ref}/{path}"
        self.objects[artifact_ref] = payload
        return {
            "artifact_id": artifact_id,
            "artifact_ref": artifact_ref,
            "sha256": digest,
            "media_type": media_type,
        }

    def get_bytes(self, artifact: Mapping[str, str]) -> bytes:
        try:
            payload = self.objects[str(artifact["artifact_ref"])]
        except KeyError as exc:
            raise KeyError(f"artifact not found: {artifact['artifact_ref']}") from exc
        actual = hashlib.sha256(payload).hexdigest()
        if actual != artifact.get("sha256"):
            raise ValueError(f"artifact sha256 mismatch: expected {artifact.get('sha256')}, got {actual}")
        return payload


class MediaRunService:
    """Executes the media-only portion of a MediaRunRequest.

    A submission is stored immediately and executed synchronously by this
    minimal service core.  The HTTP layer still returns ``202`` because a
    production worker may take the same accepted run asynchronously.  This
    keeps the external contract stable without pretending that the caller's
    media operation has already succeeded.
    """

    def __init__(
        self,
        *,
        artifact_store: ArtifactStore,
        video_adapter: ExternalVideoAdapter,
        video_source_resolver: VideoSourceResolver,
        capabilities: Mapping[str, Mapping[str, Capability]],
        audio_adapter: Optional[ExternalAudioAdapter] = None,
        audio_source_resolver: Optional[AudioSourceResolver] = None,
        capture_adapter: Optional[MediaCaptureAdapter] = None,
    ) -> None:
        self._artifact_store = artifact_store
        self._video_adapter = video_adapter
        self._video_source_resolver = video_source_resolver
        self._audio_adapter = audio_adapter
        self._audio_source_resolver = audio_source_resolver
        self._capture_adapter = capture_adapter
        self._capabilities = {key: dict(value) for key, value in capabilities.items()}
        self._runs: Dict[str, Dict[str, Any]] = {}
        self._idempotency: Dict[str, str] = {}
        self._lock = threading.RLock()

    def submit(self, request: Mapping[str, Any]) -> str:
        """Accept one request and return its stable run id.

        The idempotency key intentionally belongs to the service boundary, not
        to a provider adapter.  Retrying a delivery therefore cannot open a
        second virtual-camera session.
        """
        self._validate_request(request)
        key = str(request["idempotency_key"])
        with self._lock:
            if key in self._idempotency:
                return self._idempotency[key]
            run_id = f"media-run-{uuid.uuid4().hex}"
            self._idempotency[key] = run_id
            self._runs[run_id] = self._new_result(run_id, request)

        self._execute(run_id, request)
        return run_id

    def prepare_result(self, run_id: str, request: Mapping[str, Any]) -> Dict[str, Any]:
        """Persist the input snapshot and create a queued result without execution.

        The asynchronous worker uses this method before inserting the durable
        SQLite row.  It is intentionally separate from ``submit`` so HTTP
        acceptance never performs device I/O.
        """
        self._validate_request(request)
        return self._new_result(run_id, request)

    def execute_assigned(
        self, run_id: str, request: Mapping[str, Any], initial_result: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """Execute an already accepted run id for the asynchronous worker."""
        with self._lock:
            if run_id in self._runs:
                raise MediaRunConflictError(f"media run is already loaded: {run_id}")
            self._runs[run_id] = json.loads(json.dumps(initial_result))
        self._execute(run_id, request)
        return self.get(run_id)

    def get(self, run_id: str) -> Dict[str, Any]:
        with self._lock:
            try:
                return json.loads(json.dumps(self._runs[run_id]))
            except KeyError as exc:
                raise KeyError(f"media run not found: {run_id}") from exc

    def cancel(self, run_id: str, reason: str) -> Dict[str, Any]:
        with self._lock:
            result = self._runs.get(run_id)
            if result is None:
                raise KeyError(f"media run not found: {run_id}")
            if result["status"] not in {"queued", "running"}:
                raise MediaRunConflictError(f"cannot cancel terminal run: {run_id}")
            result["status"] = "canceled"
            result["error"] = reason
            return json.loads(json.dumps(result))

    def capabilities_for(self, environment_ref: str) -> List[Dict[str, str]]:
        return [
            capability.to_dict()
            for _, capability in sorted(self._capabilities.get(environment_ref, {}).items())
        ]

    def _new_result(self, run_id: str, request: Mapping[str, Any]) -> Dict[str, Any]:
        snapshot = self._artifact_store.put_json(f"media-runs/{run_id}/input-snapshot.json", dict(request))
        empty_index = self._artifact_store.put_json(
            f"media-runs/{run_id}/evidence-index.json", {"run_id": run_id, "artifacts": []}
        )
        return {
            "schema_version": "prismloop.media-run-result.v1",
            "run_id": run_id,
            "request_id": request["request_id"],
            "status": "queued",
            "input_snapshot": snapshot,
            "capability_report": self.capabilities_for(str(request["environment_ref"])),
            "outputs": [],
            "stream_receipts": [],
            "evidence_index": empty_index,
        }

    def _execute(self, run_id: str, request: Mapping[str, Any]) -> None:
        with self._lock:
            result = self._runs[run_id]
            result["status"] = "running"

        events: List[Dict[str, Any]] = []
        missing = self._unavailable_capabilities(request)
        if missing:
            self._store_execution_evidence(run_id, events)
            self._complete(run_id, "capability_unavailable", error="; ".join(missing))
            return

        routers: Dict[str, VideoSourceRouter] = {}
        audio_router = AudioSourceRouter(self._audio_adapter) if self._audio_adapter else None
        stream_profiles: Dict[str, StreamProfile] = {}
        staged_sources: Dict[str, Dict[str, VideoFrameSource]] = {}
        staged_audio_sources: Dict[str, AudioFrameSource] = {}
        stream_open: Dict[str, bool] = {}
        microphone_open: Dict[str, bool] = {}
        clock_ns = 0
        input_specs = {item["input_id"]: item for item in request["inputs"]}
        stream_specs = {item["stream_id"]: item for item in request.get("camera_streams", [])}

        try:
            for step in request["sequence"]:
                action = step["action"]
                events.append({"step_id": step["step_id"], "action": action, "at_ns": clock_ns})
                if action == "input.stage":
                    input_id = step["input_id"]
                    input_spec = input_specs.get(input_id)
                    if input_spec is None:
                        raise MediaRunValidationError(f"unknown input_id: {input_id}")
                    for stream_id, spec in stream_specs.items():
                        if input_spec["kind"] != "camera.video":
                            continue
                        profile = stream_profiles.setdefault(stream_id, self._profile(spec))
                        staged_sources.setdefault(stream_id, {})[input_id] = self._video_source_resolver.resolve(
                            input_spec, profile
                        )
                    if input_spec["kind"].startswith("microphone."):
                        if self._audio_source_resolver is None:
                            raise MediaRunValidationError("microphone source resolver is not configured")
                        staged_audio_sources[input_id] = self._audio_source_resolver.resolve(input_spec)
                elif action == "camera.stream.open":
                    stream_id = step["stream_id"]
                    spec = self._stream_spec(stream_specs, stream_id)
                    profile = stream_profiles.setdefault(stream_id, self._profile(spec))
                    initial_input_id = spec["initial_input_id"]
                    source = staged_sources.get(stream_id, {}).get(initial_input_id)
                    if source is None:
                        raise MediaRunValidationError(
                            f"initial input must be pre-staged before open: {initial_input_id}"
                        )
                    router = VideoSourceRouter(self._video_adapter)
                    router.open(
                        stream_id,
                        initial_input_id,
                        source,
                        profile=profile,
                        max_interframe_gap_ms=spec["max_interframe_gap_ms"],
                        end_of_source_policy=spec.get("end_of_source_policy", "loop"),
                    )
                    routers[stream_id] = router
                    stream_open[stream_id] = True
                elif action == "camera.stream.switch":
                    stream_id = step["stream_id"]
                    source_id = step["input_id"]
                    router = self._open_router(routers, stream_id)
                    source = staged_sources.get(stream_id, {}).get(source_id)
                    if source is None:
                        raise MediaRunValidationError(
                            f"switch input must be pre-staged before switch: {source_id}"
                        )
                    router.stage(stream_id, source_id, source)
                    router.switch(stream_id, source_id)
                elif action == "wait":
                    duration_ms = step["duration_ms"]
                    self._pump_open_streams(routers, stream_open, stream_profiles, duration_ms, clock_ns)
                    self._pump_open_microphones(
                        audio_router, microphone_open, staged_audio_sources, duration_ms, clock_ns
                    )
                    clock_ns += duration_ms * 1_000_000
                elif action == "input.start":
                    input_id = step["input_id"]
                    if input_specs[input_id]["kind"].startswith("microphone."):
                        if audio_router is None:
                            raise MediaRunValidationError("microphone adapter is not configured")
                        source = staged_audio_sources.get(input_id)
                        if source is None:
                            raise MediaRunValidationError(
                                f"microphone input must be pre-staged before start: {input_id}"
                            )
                        audio_router.open(input_id, source)
                        microphone_open[input_id] = True
                    else:
                        raise MediaRunValidationError(f"input.start only supports microphone inputs: {input_id}")
                elif action == "input.stop":
                    input_id = step["input_id"]
                    if microphone_open.get(input_id):
                        assert audio_router is not None
                        audio_router.close(input_id)
                        microphone_open[input_id] = False
                elif action == "camera.stream.close":
                    stream_id = step["stream_id"]
                    router = self._open_router(routers, stream_id)
                    stream_open[stream_id] = False
                    self._store_stream_receipt(run_id, router.close(stream_id).to_dict())
                elif action == "capture.screenshot":
                    captured = self._capture().capture_screenshot()
                    self._store_capture(run_id, step["step_id"], "screen.image", captured.payload, captured.media_type)
                elif action == "capture.video.start":
                    self._capture().start_screen_video()
                elif action == "capture.video.stop":
                    captured = self._capture().stop_screen_video()
                    self._store_capture(run_id, step["step_id"], "screen.video", captured.payload, captured.media_type)
                elif action == "capture.audio.start":
                    self._capture().start_speaker_audio()
                elif action == "capture.audio.stop":
                    captured = self._capture().stop_speaker_audio()
                    self._store_capture(run_id, step["step_id"], "speaker.audio", captured.payload, captured.media_type)
                else:
                    raise MediaRunValidationError(f"unsupported media action: {action}")

            for stream_id, is_open in stream_open.items():
                if is_open:
                    receipt = routers[stream_id].close(stream_id).to_dict()
                    self._store_stream_receipt(run_id, receipt)
            for input_id, is_open in microphone_open.items():
                if is_open:
                    assert audio_router is not None
                    audio_router.close(input_id)
            self._store_execution_evidence(run_id, events)
            self._complete(run_id, "completed")
        except Exception as exc:  # Preserve structured run outcome; raw adapter logs belong in storage.
            for stream_id, is_open in stream_open.items():
                if is_open:
                    try:
                        routers[stream_id].close(stream_id)
                    except Exception:
                        pass
            for input_id, is_open in microphone_open.items():
                if is_open and audio_router is not None:
                    try:
                        audio_router.close(input_id)
                    except Exception:
                        pass
            self._store_execution_evidence(run_id, events)
            self._complete(run_id, "error", error=str(exc))

    def _store_stream_receipt(self, run_id: str, receipt: Mapping[str, Any]) -> None:
        artifact = self._artifact_store.put_json(
            f"media-runs/{run_id}/stream-receipts/{receipt['stream_id']}.json", dict(receipt)
        )
        with self._lock:
            public_fields = {
                "stream_id",
                "profile",
                "emitted_frame_count",
                "max_observed_interframe_gap_ms",
                "source_switch_count",
                "source_session_restarts",
                "discontinuity_count",
                "active_source_id",
                "source_history",
            }
            self._runs[run_id]["stream_receipts"].append(
                {key: value for key, value in receipt.items() if key in public_fields} | {"artifact": artifact}
            )

    def _store_execution_evidence(self, run_id: str, events: List[Dict[str, Any]]) -> None:
        execution_log = self._artifact_store.put_json(f"media-runs/{run_id}/execution.json", events)
        with self._lock:
            result = self._runs[run_id]
            result["outputs"].append(
                {"output_id": "execution-log", "kind": "execution.log", "step_id": "service", "artifact": execution_log}
            )
            evidence = {
                "run_id": run_id,
                "artifacts": [result["input_snapshot"], execution_log]
                + [item["artifact"] for item in result["stream_receipts"]]
                + [item["artifact"] for item in result["outputs"] if item["kind"] != "execution.log"],
            }
            result["evidence_index"] = self._artifact_store.put_json(
                f"media-runs/{run_id}/evidence-index.json", evidence
            )

    def _store_capture(self, run_id: str, step_id: str, kind: str, payload: bytes, media_type: str) -> None:
        if not payload:
            raise MediaRunValidationError(f"capture adapter returned empty {kind} payload")
        extension = {
            "image/png": "png", "image/jpeg": "jpg", "video/mp4": "mp4", "video/webm": "webm", "audio/wav": "wav"
        }.get(media_type)
        if extension is None:
            raise MediaRunValidationError(f"unsupported captured media type: {media_type}")
        artifact = self._artifact_store.put_bytes(
            f"media-runs/{run_id}/outputs/{step_id}.{extension}", payload, media_type
        )
        with self._lock:
            self._runs[run_id]["outputs"].append(
                {"output_id": f"{kind}-{step_id}", "kind": kind, "step_id": step_id, "artifact": artifact}
            )

    def _capture(self) -> MediaCaptureAdapter:
        if self._capture_adapter is None:
            raise MediaRunValidationError("capture adapter is not configured")
        return self._capture_adapter

    def _complete(self, run_id: str, status: str, error: str | None = None) -> None:
        with self._lock:
            self._runs[run_id]["status"] = status
            if error:
                self._runs[run_id]["error"] = error

    def _unavailable_capabilities(self, request: Mapping[str, Any]) -> List[str]:
        required = set()
        actions = {step["action"] for step in request["sequence"]}
        if "camera.stream.open" in actions:
            required.add("camera.video.inject")
        if "camera.stream.switch" in actions:
            required.add("camera.continuous_stream_switch")
        if any(item["kind"].startswith("microphone.") for item in request["inputs"]):
            required.add("microphone.pcm.inject")
        if "capture.screenshot" in actions:
            required.add("screen.image.capture")
        if {"capture.video.start", "capture.video.stop"} & actions:
            required.add("screen.video.capture")
        if {"capture.audio.start", "capture.audio.stop"} & actions:
            required.add("speaker.audio.capture")
        declared = self._capabilities.get(str(request["environment_ref"]), {})
        return [
            f"{name}: {declared.get(name, Capability(name, 'unavailable')).state}"
            for name in sorted(required)
            if declared.get(name, Capability(name, "unavailable")).state != "verified"
        ]

    @staticmethod
    def _pump_open_streams(
        routers: Mapping[str, VideoSourceRouter],
        stream_open: Mapping[str, bool],
        profiles: Mapping[str, StreamProfile],
        duration_ms: int,
        start_ns: int,
    ) -> None:
        for stream_id, is_open in stream_open.items():
            if not is_open:
                continue
            profile = profiles[stream_id]
            frame_count = max(1, round(duration_ms * profile.fps / 1_000))
            for frame_index in range(frame_count):
                routers[stream_id].pump(
                    stream_id, now_ns=start_ns + frame_index * profile.frame_interval_ns
                )

    @staticmethod
    def _pump_open_microphones(
        router: Optional[AudioSourceRouter],
        microphone_open: Mapping[str, bool],
        sources: Mapping[str, AudioFrameSource],
        duration_ms: int,
        start_ns: int,
    ) -> None:
        if router is None:
            return
        for input_id, is_open in microphone_open.items():
            if not is_open:
                continue
            profile = sources[input_id].profile
            frame_count = max(1, round(duration_ms / profile.frame_duration_ms))
            for frame_index in range(frame_count):
                router.pump(input_id, (start_ns + frame_index * profile.frame_duration_ms * 1_000_000) // 1_000)

    @staticmethod
    def _open_router(routers: Mapping[str, VideoSourceRouter], stream_id: str) -> VideoSourceRouter:
        try:
            return routers[stream_id]
        except KeyError as exc:
            raise MediaRunValidationError(f"camera stream is not open: {stream_id}") from exc

    @staticmethod
    def _stream_spec(specs: Mapping[str, Mapping[str, Any]], stream_id: str) -> Mapping[str, Any]:
        try:
            return specs[stream_id]
        except KeyError as exc:
            raise MediaRunValidationError(f"unknown camera stream: {stream_id}") from exc

    @staticmethod
    def _profile(spec: Mapping[str, Any]) -> StreamProfile:
        return StreamProfile(**spec["profile"])

    @staticmethod
    def _validate_request(request: Mapping[str, Any]) -> None:
        required = {"schema_version", "request_id", "idempotency_key", "environment_ref", "artifact_store_ref", "inputs", "sequence"}
        allowed = required | {"timeout_seconds", "camera_streams", "recognizers"}
        missing = sorted(required - set(request))
        if missing:
            raise MediaRunValidationError(f"missing required request fields: {', '.join(missing)}")
        unexpected = sorted(set(request) - allowed)
        if unexpected:
            raise MediaRunValidationError(f"unsupported request fields: {', '.join(unexpected)}")
        if request["schema_version"] != "prismloop.media-run-request.v1":
            raise MediaRunValidationError("unsupported schema_version")
        if not isinstance(request["inputs"], list) or not isinstance(request["sequence"], list):
            raise MediaRunValidationError("inputs and sequence must be arrays")
        if not request["sequence"]:
            raise MediaRunValidationError("sequence must not be empty")

        input_ids = set()
        for input_spec in request["inputs"]:
            if not isinstance(input_spec, Mapping):
                raise MediaRunValidationError("each input must be an object")
            if set(input_spec) - {"input_id", "kind", "artifact", "audio_format"}:
                raise MediaRunValidationError("input contains unsupported fields")
            input_id = input_spec.get("input_id")
            if not isinstance(input_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", input_id):
                raise MediaRunValidationError("invalid input_id")
            if input_id in input_ids:
                raise MediaRunValidationError(f"duplicate input_id: {input_id}")
            input_ids.add(input_id)
            if input_spec.get("kind") not in {"camera.image", "camera.video", "microphone.pcm", "microphone.audio"}:
                raise MediaRunValidationError(f"unsupported input kind: {input_spec.get('kind')}")
            artifact = input_spec.get("artifact")
            if not isinstance(artifact, Mapping) or set(artifact) != {"artifact_ref", "sha256", "media_type"}:
                raise MediaRunValidationError("input artifact must contain only artifact_ref, sha256, and media_type")
            if not str(artifact["artifact_ref"]).startswith("artifact://"):
                raise MediaRunValidationError("input artifact_ref must use artifact://")
            if not re.fullmatch(r"[0-9a-f]{64}", str(artifact["sha256"])):
                raise MediaRunValidationError("input artifact sha256 must be lowercase SHA-256")
            if str(input_spec["kind"]).startswith("microphone."):
                audio_format = input_spec.get("audio_format")
                if not isinstance(audio_format, Mapping):
                    raise MediaRunValidationError("microphone inputs require audio_format")
                try:
                    AudioProfile(**audio_format)
                except (TypeError, ValueError) as exc:
                    raise MediaRunValidationError(f"invalid microphone audio_format: {exc}") from exc

        stream_specs = {item.get("stream_id"): item for item in request.get("camera_streams", []) if isinstance(item, Mapping)}
        if len(stream_specs) != len(request.get("camera_streams", [])):
            raise MediaRunValidationError("camera_stream ids must be unique objects")
        for stream_id, spec in stream_specs.items():
            if not isinstance(stream_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", stream_id):
                raise MediaRunValidationError("invalid camera stream_id")
            if spec.get("initial_input_id") not in input_ids:
                raise MediaRunValidationError("camera stream initial_input_id is not a declared input")
            try:
                StreamProfile(**spec["profile"])
            except (KeyError, TypeError, ValueError) as exc:
                raise MediaRunValidationError(f"invalid camera stream profile: {exc}") from exc

        actions = {
            "input.stage", "input.start", "input.stop", "camera.stream.open", "camera.stream.switch", "camera.stream.close",
            "capture.screenshot", "capture.video.start", "capture.video.stop", "capture.audio.start", "capture.audio.stop", "wait",
        }
        for step in request["sequence"]:
            if not isinstance(step, Mapping) or set(step) - {"step_id", "action", "input_id", "stream_id", "duration_ms"}:
                raise MediaRunValidationError("sequence step contains unsupported fields")
            action = step.get("action")
            if action not in actions:
                raise MediaRunValidationError(f"unsupported media action: {action}")
            if action in {"input.stage", "input.start", "input.stop"} and step.get("input_id") not in input_ids:
                raise MediaRunValidationError(f"{action} requires a declared input_id")
            if action in {"camera.stream.open", "camera.stream.close", "camera.stream.switch"} and step.get("stream_id") not in stream_specs:
                raise MediaRunValidationError(f"{action} requires a declared stream_id")
            if action == "camera.stream.switch" and step.get("input_id") not in input_ids:
                raise MediaRunValidationError("camera.stream.switch requires a declared input_id")
            if action == "wait" and (not isinstance(step.get("duration_ms"), int) or step["duration_ms"] < 1):
                raise MediaRunValidationError("wait requires positive integer duration_ms")
