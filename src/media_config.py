"""Non-secret configuration for the standalone Media I/O Harness process."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MediaServiceSettings:
    listen_host: str
    listen_port: int
    state_db_path: str
    artifact_store_ref: str
    device_provider_profile: str

    @classmethod
    def from_env(cls) -> "MediaServiceSettings":
        port = int(os.environ.get("PRISMLOOP_MEDIA_LISTEN_PORT", "8787"))
        if not 1 <= port <= 65535:
            raise ValueError("PRISMLOOP_MEDIA_LISTEN_PORT must be between 1 and 65535")
        state_db_path = os.environ.get("PRISMLOOP_STATE_DB_PATH", "data/media-runs.sqlite")
        if not state_db_path or Path(state_db_path).is_absolute() and state_db_path == "/":
            raise ValueError("PRISMLOOP_STATE_DB_PATH must name a SQLite database file")
        artifact_store_ref = os.environ.get("PRISMLOOP_ARTIFACT_STORE_REF", "tos/prismloop-media")
        provider_profile = os.environ.get("PRISMLOOP_DEVICE_PROVIDER_PROFILE", "volc/pod-raw-frame")
        if not artifact_store_ref or not provider_profile:
            raise ValueError("artifact store ref and device provider profile must be non-empty")
        return cls(
            listen_host=os.environ.get("PRISMLOOP_MEDIA_LISTEN_HOST", "127.0.0.1"),
            listen_port=port,
            state_db_path=state_db_path,
            artifact_store_ref=artifact_store_ref,
            device_provider_profile=provider_profile,
        )
