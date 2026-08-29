"""Artifact persistence and verified retrieval for media fixtures and evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Protocol


class ArtifactIntegrityError(RuntimeError):
    """An artifact was absent, malformed, or did not match its declared digest."""


class BinaryArtifactStore(Protocol):
    def put_bytes(self, path: str, payload: bytes, media_type: str) -> Dict[str, str]:
        ...

    def put_json(self, path: str, value: Mapping[str, Any] | list[Any]) -> Dict[str, str]:
        ...

    def get_bytes(self, artifact: Mapping[str, str]) -> bytes:
        ...


class FilesystemArtifactStore:
    """Local stand-in retaining the same immutable artifact semantics as TOS."""

    def __init__(self, root: str, store_ref: str = "filesystem/prismloop-media") -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._store_ref = store_ref.strip("/")

    def put_bytes(self, path: str, payload: bytes, media_type: str) -> Dict[str, str]:
        relative = self._safe_path(path)
        destination = self._root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        return self._artifact(path, digest, media_type)

    def put_json(self, path: str, value: Mapping[str, Any] | list[Any]) -> Dict[str, str]:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return self.put_bytes(path, payload, "application/json")

    def get_bytes(self, artifact: Mapping[str, str]) -> bytes:
        path = self._artifact_path(artifact)
        try:
            payload = (self._root / self._safe_path(path)).read_bytes()
        except FileNotFoundError as exc:
            raise ArtifactIntegrityError(f"artifact not found: {artifact['artifact_ref']}") from exc
        self._verify(payload, artifact)
        return payload

    def _artifact_path(self, artifact: Mapping[str, str]) -> str:
        prefix = f"artifact://{self._store_ref}/"
        artifact_ref = str(artifact["artifact_ref"])
        if not artifact_ref.startswith(prefix):
            raise ArtifactIntegrityError("artifact_ref does not belong to this artifact store")
        return artifact_ref.removeprefix(prefix)

    def _artifact(self, path: str, digest: str, media_type: str) -> Dict[str, str]:
        return {
            "artifact_id": f"art-{digest[:16]}",
            "artifact_ref": f"artifact://{self._store_ref}/{path}",
            "sha256": digest,
            "media_type": media_type,
        }

    @staticmethod
    def _safe_path(path: str) -> Path:
        relative = Path(path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ArtifactIntegrityError("artifact path must be relative and cannot traverse parent directories")
        return relative

    @staticmethod
    def _verify(payload: bytes, artifact: Mapping[str, str]) -> None:
        actual = hashlib.sha256(payload).hexdigest()
        if actual != artifact.get("sha256"):
            raise ArtifactIntegrityError(f"artifact sha256 mismatch: expected {artifact.get('sha256')}, got {actual}")


class TOSArtifactStore:
    """TOS adapter with SDK transport injected by the deployment bootstrap.

    This class intentionally avoids assuming a particular Python TOS SDK.  The
    bootstrap supplies authenticated callbacks after it has resolved credentials
    from Keychain or the environment; run requests never carry those values.
    """

    def __init__(
        self,
        *,
        store_ref: str,
        put_object: Callable[[str, bytes], None],
        get_object: Callable[[str], bytes],
        key_prefix: str = "",
    ) -> None:
        self._store_ref = store_ref.strip("/")
        self._put_object = put_object
        self._get_object = get_object
        self._key_prefix = key_prefix.strip("/")

    def put_bytes(self, path: str, payload: bytes, media_type: str) -> Dict[str, str]:
        key = self._key(path)
        self._put_object(key, payload)
        digest = hashlib.sha256(payload).hexdigest()
        return {
            "artifact_id": f"art-{digest[:16]}",
            "artifact_ref": f"artifact://{self._store_ref}/{path}",
            "sha256": digest,
            "media_type": media_type,
        }

    def put_json(self, path: str, value: Mapping[str, Any] | list[Any]) -> Dict[str, str]:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return self.put_bytes(path, payload, "application/json")

    def get_bytes(self, artifact: Mapping[str, str]) -> bytes:
        prefix = f"artifact://{self._store_ref}/"
        artifact_ref = str(artifact["artifact_ref"])
        if not artifact_ref.startswith(prefix):
            raise ArtifactIntegrityError("artifact_ref does not belong to this TOS store")
        path = artifact_ref.removeprefix(prefix)
        payload = self._get_object(self._key(path))
        FilesystemArtifactStore._verify(payload, artifact)
        return payload

    def _key(self, path: str) -> str:
        safe_path = str(FilesystemArtifactStore._safe_path(path))
        return f"{self._key_prefix}/{safe_path}" if self._key_prefix else safe_path
